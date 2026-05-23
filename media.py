# media.py — 流信息模型、Packet 增量分析器、ffprobe CSV 扫描
#
# 核心流程：
#   parse_ffprobe_streams()  → 获取流列表（JSON，数据量小）
#   scan_all_packets()        → 一次 ffprobe 获取所有 packet（CSV 流式）
#   StreamAnalyzer.feed()     → 逐 packet 增量分析，不存内存
#   StreamAnalyzer.finalize() → 汇总 issue/warning

import os
import sys
import json
import csv
import time
import queue
import fractions
import threading
import subprocess
from collections import deque

from config import (
    STREAMING_EXTENSIONS, GAP_THRESHOLD_DURATION_MS, AUDIO_GAP_THRESHOLD_DURATION_MS,
    TRUNCATION_THRESHOLD_SECONDS, SHORT_FILE_TRUNCATION_THRESHOLD_SECONDS,
    SHORT_FILE_DURATION_SECONDS, PTS_BACKWARD_THRESHOLD_SEC, VFR_GAP_THRESHOLD_SEC,
    STAGNATION_WATCHDOG_SEC, MAX_PACKET_SCAN_PER_STREAM,
    MAX_PACKET_SCAN_GLOBAL, MAX_ISSUES_PER_STREAM, PIPE_BUFFER_SIZE, MAX_LINE_LENGTH,
    STOP_EVENT, debug,
)
import config  # 运行时可能被 main.py 修改 FAST_PACKET_SCAN_SECONDS
from limiter import PacketActivity


# ===================== 流信息模型 =====================
class StreamInfo:
    """从 ffprobe JSON 提取的单条流元信息。"""
    def __init__(self, index, codec_type, codec_name, tb, afr, dur, nbf, spec):
        self.index = index                  # ffmpeg 原始 stream_index
        self.codec_type = codec_type        # 'video' 或 'audio'
        self.codec_name = codec_name        # 如 'h264', 'aac'
        self.time_base = tb                 # fractions.Fraction
        self.avg_frame_rate = afr           # fractions.Fraction 或 None
        self.duration_seconds = dur         # float 或 None（可能从 format.duration 回填）
        self.nb_frames = nbf               # int 或 None
        self.stream_spec = spec             # ffprobe 选择器格式 'v:0' / 'a:0'


def ffmpeg_map_arg(stream_info):
    return f"0:{stream_info.index}"


# ===================== 流解析 =====================
def parse_ffprobe_streams(file_path):
    cmd = [
        'ffprobe', '-v', 'quiet', '-print_format', 'json',
        '-show_streams', '-show_format', file_path
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            stdin=subprocess.DEVNULL,
            encoding='utf-8', errors='replace', timeout=30
        )
        proc.check_returncode()
        if not proc.stdout.strip():
            raise RuntimeError("ffprobe 输出为空")
        data = json.loads(proc.stdout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffprobe 超时")
    except subprocess.CalledProcessError as e:
        hint = (e.stderr[:200] if e.stderr else str(e))
        raise RuntimeError(f"ffprobe 执行失败: {hint}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ffprobe 输出解析失败: {e}")

    format_duration = None
    fmt = data.get('format', {})
    if fmt:
        dur_str = fmt.get('duration')
        if dur_str and dur_str.lower() != 'n/a':
            try:
                format_duration = float(dur_str)
            except (ValueError, TypeError):
                pass

    streams = []
    vid_idx, aud_idx = 0, 0
    for s in data.get('streams', []):
        try:
            if s.get('codec_type') not in ('video', 'audio'):
                continue
            if s.get('disposition', {}).get('attached_pic') == 1:
                continue
            ctype = s['codec_type']
            cname = s.get('codec_name', 'unknown')
            tb_str = s.get('time_base', '1/1')
            try:
                tb = fractions.Fraction(tb_str)
            except Exception:
                tb = fractions.Fraction(1, 1000)
            afr = None
            if ctype == 'video':
                afr_str = s.get('avg_frame_rate', '0/0')
                if afr_str and afr_str.lower() != 'n/a' and not afr_str.startswith('0/0'):
                    try:
                        afr = fractions.Fraction(afr_str)
                    except (ValueError, ZeroDivisionError):
                        pass
            dur = None
            dur_str = s.get('duration')
            if dur_str and dur_str.lower() != 'n/a':
                try:
                    dur = float(dur_str)
                except Exception:
                    pass
            nbf = None
            nb_str = s.get('nb_frames')
            if nb_str and nb_str.lower() != 'n/a':
                try:
                    nbf = int(nb_str)
                except Exception:
                    pass
            spec = f'v:{vid_idx}' if ctype == 'video' else f'a:{aud_idx}'
            if ctype == 'video':
                vid_idx += 1
            else:
                aud_idx += 1
            streams.append(StreamInfo(
                s.get('index', -1), ctype, cname, tb, afr, dur, nbf, spec
            ))
        except Exception:
            continue

    if format_duration:
        for st in streams:
            if st.duration_seconds is None:
                st.duration_seconds = format_duration

    return streams


# ===================== stream 增量分析器 =====================
class StreamAnalyzer:
    """逐 packet 增量分析，不存储任何 packet 到内存。
    
    三种检测：
        DTS/PTS 回退 — 视频检测 DTS、音频检测 PTS
        疑似缺帧      — PTS 间隔超过预期
        疑似截断      — 末尾 PTS 与容器时长差距过大
    """
    __slots__ = (
        'stream_info', 'tb_float', 'is_video', 'gap_thresh',
        'prev_pts', 'prev_dts', 'prev_dur',
        'cnt', 'sum_diff', 'diff_cnt', 'max_end_pts',
        'issues', 'warnings', '_issue_count', '_suppressed'
    )

    def __init__(self, stream_info):
        self.stream_info = stream_info
        self.tb_float = float(stream_info.time_base)
        if self.tb_float <= 0:
            self.tb_float = 0.001
        self.is_video = (stream_info.codec_type == 'video')
        self.gap_thresh = (
            AUDIO_GAP_THRESHOLD_DURATION_MS if not self.is_video
            else GAP_THRESHOLD_DURATION_MS
        )

        self.prev_pts = None
        self.prev_dts = None
        self.prev_dur = None
        self.cnt = 0
        self.sum_diff = 0
        self.diff_cnt = 0
        self.max_end_pts = None

        self.issues = []
        self.warnings = []
        self._issue_count = 0
        self._suppressed = False

    def _add_issue(self, msg):
        if self._issue_count < MAX_ISSUES_PER_STREAM:
            self.issues.append(msg)
        elif not self._suppressed:
            self.issues.append(
                f"[后续问题已抑制] {self.stream_info.stream_spec} "
                f"已达{MAX_ISSUES_PER_STREAM}条上限"
            )
            self._suppressed = True
        self._issue_count += 1

    def feed(self, pkt):
        pts = dts = dur = None

        try:
            v = pkt.get('pts')
            if v is not None:
                pts = int(v)
        except (ValueError, TypeError):
            pass
        try:
            v = pkt.get('dts')
            if v is not None:
                dts = int(v)
        except (ValueError, TypeError):
            pass
        try:
            v = pkt.get('duration')
            if v is not None:
                d = int(v)
                if d >= 0:
                    dur = d
        except (ValueError, TypeError):
            pass

        if pts is None:
            return

        self.cnt += 1

        # 状态更新（max_end_pts/prev_* 即使在 suppressed 模式下也要保持，
        # 否则 truncation 检测会基于过时的 max_end_pts 误报截断）
        end_pts = pts + dur if dur is not None else pts
        if self.max_end_pts is None or end_pts > self.max_end_pts:
            self.max_end_pts = end_pts

        if not self._suppressed:

            if self.is_video:
                if (
                    self.prev_dts is not None
                    and dts is not None
                    and dts < self.prev_dts
                ):
                    backward_sec = (self.prev_dts - dts) * self.tb_float
                    if backward_sec > PTS_BACKWARD_THRESHOLD_SEC:
                        self._add_issue(
                            f"[DTS回退] {self.stream_info.stream_spec} "
                            f"帧{self.cnt} {backward_sec:.3f}s"
                        )
            else:
                if self.prev_pts is not None and pts < self.prev_pts:
                    backward_sec = (self.prev_pts - pts) * self.tb_float
                    if backward_sec > PTS_BACKWARD_THRESHOLD_SEC:
                        self._add_issue(
                            f"[PTS回退] {self.stream_info.stream_spec} "
                            f"帧{self.cnt} {backward_sec:.3f}s"
                        )

            if self.prev_pts is not None:
                if self.prev_dur is not None:
                    expected = self.prev_pts + self.prev_dur
                    jitter = max(
                        int(self.gap_thresh / 1000.0 / self.tb_float),
                        self.prev_dur * 3
                    )
                    if pts > expected + jitter:
                        gap = (pts - expected) * self.tb_float
                        if self.is_video:
                            if gap > VFR_GAP_THRESHOLD_SEC:
                                self._add_issue(
                                    f"[疑似缺帧] {self.stream_info.stream_spec} "
                                    f"帧{self.cnt} 间隔 {gap:.3f}s"
                                )
                        else:
                            self._add_issue(
                                f"[疑似缺帧] {self.stream_info.stream_spec} "
                                f"帧{self.cnt} 间隔 {gap:.3f}s"
                            )
                else:
                    diff = pts - self.prev_pts
                    if diff > 0:
                        self.sum_diff += diff
                        self.diff_cnt += 1

        self.prev_pts = pts
        self.prev_dts = dts
        self.prev_dur = dur

    def finalize(self, ext):
        if self.cnt == 0:
            self.issues.append(
                f"[空流] {self.stream_info.stream_spec} 无帧数据"
            )
            return self.issues, self.warnings

        if self.diff_cnt > 1:
            avg = self.sum_diff / self.diff_cnt
            self.warnings.append(
                f"[注意] {self.stream_info.stream_spec} "
                f"缺duration，平均PTS步长={avg:.1f}"
            )

        if (
            ext and ext not in STREAMING_EXTENSIONS
            and self.stream_info.duration_seconds
            and self.max_end_pts is not None
        ):
            stream_end = self.max_end_pts * self.tb_float
            diff = self.stream_info.duration_seconds - stream_end

            if self.stream_info.duration_seconds < SHORT_FILE_DURATION_SECONDS:
                trunc_thresh = SHORT_FILE_TRUNCATION_THRESHOLD_SECONDS
            else:
                trunc_thresh = TRUNCATION_THRESHOLD_SECONDS

            if diff > trunc_thresh:
                self.issues.append(
                    f"[疑似截断] {self.stream_info.stream_spec} "
                    f"容器{self.stream_info.duration_seconds:.3f}s "
                    f"实际{stream_end:.3f}s 差{diff:.3f}s"
                )

        return self.issues, self.warnings


# ===================== IO helper =====================
def kill_process(proc):
    """强制终止子进程及其所有子进程。
    
    Windows: CTRL_BREAK → taskkill /F /T
    Linux:   SIGKILL to process group → fallback to single process
    """
    if proc.poll() is not None:
        return
    try:
        if os.name == 'nt':
            try:
                proc.send_signal(subprocess.CTRL_BREAK_EVENT)
                time.sleep(0.5)
            except Exception:
                pass
            for _ in range(2):
                try:
                    subprocess.run(
                        ['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                        capture_output=True, timeout=5
                    )
                    break
                except subprocess.TimeoutExpired:
                    continue
        else:
            try:
                import signal
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                return
            except Exception:
                try:
                    import signal
                    os.kill(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
    except Exception as e:
        print(f"[kill_process失败] {e}", file=sys.stderr)

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        print(f"[警告] 进程 {proc.pid} 强制终止超时", file=sys.stderr)


# ===================== packet 读取 =====================
def packet_reader(proc, out_queue, activity):
    try:
        while True:
            raw = proc.stdout.readline()
            if raw == '':
                break

            activity.touch()

            if STOP_EVENT.is_set():
                break

            line = raw.strip()
            if not line:
                continue
            if len(line) > MAX_LINE_LENGTH:
                continue

            try:
                cols = next(csv.reader([line]))
                if len(cols) < 4:
                    continue

                fields = {
                    'stream_index': cols[0],
                    'pts': cols[1],
                    'dts': cols[2],
                    'duration': cols[3],
                }

                try:
                    while not STOP_EVENT.is_set():
                        try:
                            out_queue.put(('packet', fields), timeout=1)
                            break
                        except queue.Full:
                            continue
                except Exception:
                    pass

            except Exception:
                continue

    except Exception as e:
        out_queue.put(('error', str(e)))

    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        out_queue.put(('done', None))


def stderr_reader(pipe, lines, activity=None):
    # ffmpeg -progress pipe:2 每秒输出大量进度行，应过滤以保留真实错误
    _ignore = ('frame=', 'fps=', 'stream_', 'bitrate=', 'total_size=',
               'out_time_', 'progress=', 'speed=', 'dup_frames=', 'drop_frames=')
    try:
        for line in pipe:
            if STOP_EVENT.is_set():
                break
            if activity:
                activity.touch()
            if len(line) > 4096:
                line = line[:4096]
            stripped = line.strip()
            if stripped.startswith(_ignore):
                continue
            lines.append(line)
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def scan_all_packets(file_path, ext, streams):
    """单次 ffprobe 运行，CSV 流式读取所有 packet，增量分析所有 stream。
    
    不将 packet 存入内存。每条 CSV 行立即分发给对应 stream_index 的 StreamAnalyzer。
    受 MAX_PACKET_SCAN_PER_STREAM 和 MAX_PACKET_SCAN_GLOBAL 双重上限保护。
    """
    analyzers = {s.index: StreamAnalyzer(s) for s in streams}
    per_stream_counts = {s.index: 0 for s in streams}
    scan_warnings = []

    cmd = ['ffprobe', '-hide_banner', '-v', 'error']

    if ext in STREAMING_EXTENSIONS and config.FAST_PACKET_SCAN_SECONDS > 0:
        cmd += ['-read_intervals', f'%+{config.FAST_PACKET_SCAN_SECONDS}']

    cmd += [
        '-show_packets',
        '-show_entries', 'packet=stream_index,pts,dts,duration',
        '-of', 'csv=p=0',
        file_path
    ]

    try:
        popen_kwargs = {
            'stdin': subprocess.DEVNULL,
            'stdout': subprocess.PIPE,
            'stderr': subprocess.PIPE,
            'text': True,
            'encoding': 'utf-8',
            'errors': 'replace',
            'bufsize': 1,
        }
        if os.name == 'nt':
            popen_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs['start_new_session'] = True
            if sys.version_info >= (3, 10):
                popen_kwargs['pipesize'] = PIPE_BUFFER_SIZE

        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except (PermissionError, OSError):
            if 'pipesize' in popen_kwargs:
                del popen_kwargs['pipesize']
            proc = subprocess.Popen(cmd, **popen_kwargs)
    except Exception as e:
        scan_warnings.append(f"[ffprobe启动失败] {e}")
        return {si: ([], []) for si in analyzers}, scan_warnings

    stderr_lines = deque(maxlen=200)
    stderr_thread = threading.Thread(
        target=stderr_reader, args=(proc.stderr, stderr_lines), daemon=True
    )
    stderr_thread.start()

    activity = PacketActivity()
    q = queue.Queue(maxsize=10000)
    reader_thread = threading.Thread(
        target=packet_reader, args=(proc, q, activity), daemon=True
    )
    reader_thread.start()

    total_packets = 0

    while True:
        if STOP_EVENT.is_set():
            kill_process(proc)
            break

        if activity.age() > STAGNATION_WATCHDOG_SEC:
            kill_process(proc)
            scan_warnings.append("[ffprobe输出停滞] 损坏文件导致无限循环")
            break

        try:
            item = q.get(timeout=0.5)
        except queue.Empty:
            continue

        if item[0] == 'done':
            break
        if item[0] == 'error':
            scan_warnings.append(f"[ffprobe包解析错误] {item[1]}")
            continue

        pkt = item[1]
        try:
            si = int(pkt.get('stream_index', -1))
        except (ValueError, TypeError):
            continue

        if si not in per_stream_counts:
            continue

        per_stream_counts[si] += 1
        if per_stream_counts[si] == MAX_PACKET_SCAN_PER_STREAM + 1:
            scan_warnings.append(
                f"[单流上限] stream {si} 达到 {MAX_PACKET_SCAN_PER_STREAM} 包上限，停止分析"
            )
        if per_stream_counts[si] > MAX_PACKET_SCAN_PER_STREAM:
            continue

        total_packets += 1

        analyzer = analyzers.get(si)
        if analyzer:
            analyzer.feed(pkt)

        if total_packets >= MAX_PACKET_SCAN_GLOBAL:
            scan_warnings.append(
                f"达到全局最大packet扫描数({MAX_PACKET_SCAN_GLOBAL})"
            )
            kill_process(proc)
            break

    reader_thread.join(timeout=2)
    if reader_thread.is_alive():
        scan_warnings.append("[packet_reader 线程未正常退出]")

    try:
        proc.stderr.close()
    except Exception:
        pass
    stderr_thread.join(timeout=2)

    try:
        proc.wait(timeout=5)
    except Exception:
        kill_process(proc)

    err_text = ''.join(stderr_lines).strip()
    if err_text and proc.returncode != 0:
        scan_warnings.append(
            f"[ffprobe stderr] {err_text[:500]}"
        )

    results = {}
    for si, analyzer in analyzers.items():
        results[si] = analyzer.finalize(ext)
        debug(2, "[扫描] stream_index=%d: %d 个包  %d issue",
              si, analyzer.cnt, analyzer._issue_count)

    return results, scan_warnings
