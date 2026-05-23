# check.py — 文件校验器、文件收集器、批量执行器
#
# check_file()   : 单个文件全量检测（图片/视频/音频分发）
# check_image()  : 图片完整性快速检测
# collect_files(): 遍历目录收集媒体文件
# run_checks()   : 多线程批量执行 + 结果回写数据库

import os
import sys
import time
import threading
import subprocess

from config import (
    IMAGE_EXTENSIONS, AUDIO_EXTENSIONS, STREAMING_EXTENSIONS,
    STOP_EVENT, debug,
)
from media import parse_ffprobe_streams, scan_all_packets
from decode import decode_with_fallback


def check_image(file_path):
    issues = []
    warnings = []
    cmd = [
        'ffmpeg', '-nostdin', '-hide_banner', '-v', 'error',
        '-i', file_path,
        '-f', 'null', '-'
    ]
    try:
        proc = subprocess.run(
            cmd, stdin=subprocess.DEVNULL,
            capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=60
        )
        if proc.returncode != 0:
            issues.append(f"[图片损坏] {proc.stderr.strip()[:500]}")
        elif proc.stderr.strip():
            warnings.append(f"[图片警告] {proc.stderr.strip()[:200]}")
    except subprocess.TimeoutExpired:
        issues.append("[图片超时] ffmpeg 解码超过 60s")
    except Exception as e:
        issues.append(f"[图片检测异常] {e}")
    return issues, warnings


def check_file(file_path, hw_methods, do_decode, gpu_idx):
    result = {'file': file_path, 'issues': [], 'warnings': []}
    bname = os.path.basename(file_path)
    ext = os.path.splitext(file_path)[1].lower()

    if ext in IMAGE_EXTENSIONS:
        if STOP_EVENT.is_set():
            result['issues'].append("检查被中断")
            return result
        debug(1, "[开始] %s (image)", bname)
        t0 = time.monotonic()
        iss, wrn = check_image(file_path)
        result['issues'].extend(iss)
        result['warnings'].extend(wrn)
        elapsed = time.monotonic() - t0
        status = 'FAIL' if iss else ('WARN' if wrn else 'OK')
        debug(1, "[完成] %s: %s (%.1fs)%s", bname, status, elapsed,
              ' | ' + iss[0][:60] if iss else (' | ' + wrn[0][:60] if wrn else ''))
        return result

    if STOP_EVENT.is_set():
        result['issues'].append("检查被中断")
        return result
    debug(1, "[开始] %s", bname)

    try:
        fsize = os.path.getsize(file_path)
        if fsize <= 0:
            result['issues'].append("文件大小为0，无效媒体")
            return result
        if fsize < 512:
            result['issues'].append(f"文件过小({fsize}B)，不可能是有效媒体")
            return result
        if ext in STREAMING_EXTENSIONS and fsize < 1024 * 1024:
            result['warnings'].append("流媒体文件体积过小，存在损坏风险")
    except OSError as e:
        result['issues'].append(f"文件访问失败: {e}")
        return result

    start_file = time.monotonic()

    try:
        streams = parse_ffprobe_streams(file_path)
    except Exception as e:
        result['issues'].append(f"无法获取流信息: {e}")
        debug(1, "[流解析失败] %s: %s", bname, e)
        return result
    if not streams:
        result['issues'].append("未检测到有效音视频流")
        return result
    debug(1, "[流] %s: %d 个流 (%s)", bname, len(streams),
          ', '.join(s.stream_spec for s in streams))

    scan_results, scan_warnings = scan_all_packets(file_path, ext, streams)
    result['warnings'].extend(scan_warnings)

    if STOP_EVENT.is_set():
        result['issues'].append("检查被中断")
        return result

    for st in streams:
        iss, wrn = scan_results.get(st.index, ([], []))
        result['issues'].extend(iss)
        result['warnings'].extend(wrn)
        debug(2, "[pkt] %s %s: %d 个 issue, %d 个 warning",
              bname, st.stream_spec, len(iss), len(wrn))

    # 检测到截断/空流等结构性问题时跳过解码：ffmpeg 可能在截断处无限等待
    has_fatal = any('截断' in s or '空流' in s for s in result['issues'])
    if do_decode and not STOP_EVENT.is_set():
        if has_fatal:
            debug(1, "[跳过解码] %s: 已检测到结构性问题，不再尝试解码", bname)
        else:
            for st in streams:
                if STOP_EVENT.is_set():
                    break
                debug(1, "[decode] %s %s 开始 (codec=%s) %s", bname, st.stream_spec,
                      st.codec_name, file_path)
                dec_iss, dec_wrn, interrupted = decode_with_fallback(
                    file_path, st, hw_methods, gpu_idx
                )
                if interrupted:
                    result['issues'].append("[中断] 解码被用户中止")
                    break
                result['issues'].extend(dec_iss)
                result['warnings'].extend(dec_wrn)
                debug(1, "[decode] %s %s 完成: %d issue, %d warning",
                      bname, st.stream_spec, len(dec_iss), len(dec_wrn))

    elapsed = time.monotonic() - start_file
    status = ('FAIL' if result['issues'] else
              ('WARN' if result['warnings'] else 'OK'))
    detail = ''
    if result['issues']:
        detail = ' | ' + result['issues'][0][:60]
    elif result['warnings']:
        detail = ' | ' + result['warnings'][0][:60]
    debug(1, "[完成] %s: %s (%.1fs)%s", bname, status, elapsed, detail)
    return result


def collect_files(root, exts):
    """遍历 root 目录收集所有符合后缀的文件。
    使用 os.scandir 减少系统调用。
    目录和文件均使用 (st_dev, st_ino) 去重，
    防止 bind mount/硬链接导致同一文件被重复检测。
    st_ino == 0 时（Windows FAT/exFAT/部分网络盘）跳过 inode 去重。"""
    files = []
    seen_inodes = set()
    dir_stack = [root]

    while dir_stack:
        current = dir_stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        st = entry.stat()
                        if st.st_ino != 0:
                            inode_key = (st.st_dev, st.st_ino)
                            if inode_key in seen_inodes:
                                continue
                            seen_inodes.add(inode_key)
                    except OSError:
                        continue

                    if entry.is_dir(follow_symlinks=False):
                        dir_stack.append(entry.path)
                    elif os.path.splitext(entry.name)[1].lower() in exts:
                        files.append(entry.path)
        except OSError:
            continue

    return files


def run_checks(file_list, hw_methods, do_decode, workers,
               checkpoint, display, gpu_idx, limiter=None):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from config import AUDIO_EXTENSIONS, IMAGE_EXTENSIONS

    results = []
    future_map = {}

    def _worker(path):
        ext = os.path.splitext(path)[1].lower()
        cpu_heavy = ext in AUDIO_EXTENSIONS or ext in IMAGE_EXTENSIONS
        small_file = ext in IMAGE_EXTENSIONS or ext in AUDIO_EXTENSIONS
        if limiter:
            limiter.acquire(cpu_heavy=cpu_heavy, small_file=small_file)
        try:
            return check_file(path, hw_methods, do_decode, gpu_idx)
        finally:
            if limiter:
                limiter.release(cpu_heavy=cpu_heavy, small_file=small_file)

    max_pending = max(workers * 4, len(file_list)) if file_list else 1
    submit_sem = threading.BoundedSemaphore(max_pending)

    def _worker_bounded(path):
        try:
            return _worker(path)
        finally:
            submit_sem.release()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for path in file_list:
            submit_sem.acquire()
            fut = ex.submit(_worker_bounded, path)
            future_map[fut] = path
        done = 0
        try:
            for fut in as_completed(future_map):
                path = future_map[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    res = {
                        'file': path,
                        'issues': [f"线程异常:{e}"],
                        'warnings': []
                    }
                status = (
                    'failure' if res['issues']
                    else ('warning' if res['warnings'] else 'success')
                )
                interrupted = any('中断' in s for s in res['issues']) or STOP_EVENT.is_set()
                bname = os.path.basename(path)
                if interrupted:
                    msg = f"{bname} - 已中断（下次恢复）"
                    display.add(msg)
                else:
                    checkpoint.enqueue(path, status, res['issues'], res['warnings'])
                    if status == 'failure':
                        first = res['issues'][0]
                        msg = f"{bname} - 失败 | {first[:80]}"
                    elif status == 'warning':
                        first = res['warnings'][0] if res['warnings'] else ''
                        msg = f"{bname} - 警告 | {first[:80]}"
                    else:
                        msg = f"{bname} - 校验通过"
                    display.add(msg)
                done += 1
                display.update(done)
                display.draw()
                results.append(res)
        except KeyboardInterrupt:
            print("\n[用户中断] 正在清理资源...")
            STOP_EVENT.set()
            try:
                ex.shutdown(wait=True, cancel_futures=True)
            except TypeError:
                ex.shutdown(wait=True)
    display.flush()
    return results
