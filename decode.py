# decode.py — ffmpeg 解码与硬件加速降级
#
# 解码链路：
#   build_decode_command() → 构造 ffmpeg 命令（含 -progress pipe:2 输出进度）
#   run_decode()           → 执行解码，通过 stderr 活性检测停滞（没有固定超时）
#   decode_with_fallback() → NVDEC → QSV → CPU 逐级降级

import os
import sys
import time
import shlex
import subprocess
import threading
from collections import deque

from config import (
    PIPE_BUFFER_SIZE, STAGNATION_WATCHDOG_SEC, STOP_EVENT, debug,
    NVDEC_CODECS, QSV_CODECS,
)
from media import ffmpeg_map_arg, stderr_reader, kill_process
from limiter import PacketActivity


def build_decode_command(file_path, stream_info, accel, device_idx=None):
    cmd = [
        'ffmpeg',
        '-thread_queue_size', '4096',
        '-v', 'error',
        '-err_detect', 'explode',
        '-progress', 'pipe:2',
    ]

    if accel == 'qsv':
        cmd += [
            '-init_hw_device', 'qsv=hw',
            '-hwaccel', 'qsv',
            '-hwaccel_output_format', 'qsv',
            '-threads', '1'
        ]
    elif accel == 'cuda':
        cmd += ['-hwaccel', 'cuda']
        if device_idx is not None:
            cmd += ['-hwaccel_device', str(device_idx)]
        cmd += ['-threads', '1']
    else:
        cmd += ['-threads', 'auto']

    cmd += [
        '-i', file_path,
        '-map', ffmpeg_map_arg(stream_info),
        '-sn',
        '-dn',
        '-f', 'null',
        '-'
    ]

    return cmd


def is_hw_init_fatal(err):
    if not err:
        return False
    lower = err.lower()
    keywords = [
        "decoder surfaces", "resource temporarily unavailable",
        "device creation failed", "no capable devices", "no device",
        "cannot open device", "failed to create decoder",
        "hwaccel initialization", "no supported child device",
        "out of memory", "insufficient resources",
        "driver error", "cuda error", "opencl error",
        "vaapi error", "drm error"
    ]
    return any(k in lower for k in keywords)


def run_decode(cmd):
    """执行一次 ffmpeg 解码，返回 (err, hw_fail, interrupted)。
    
    没有固定超时 — 依赖 stderr 活性检测：stderr 持续输出则不杀，
    只有 stderr 静默超过 STAGNATION_WATCHDOG_SEC 才判定卡死。
    """
    try:
        popen_kwargs = {
            'stdin': subprocess.DEVNULL,
            'stdout': subprocess.DEVNULL,
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
        return f"启动失败: {e}", False, False

    activity = PacketActivity()
    stderr_lines = deque(maxlen=500)
    stderr_thread = threading.Thread(
        target=stderr_reader,
        args=(proc.stderr, stderr_lines, activity),
        daemon=True
    )
    stderr_thread.start()

    err_text = ""
    interrupted = False
    try:
        while True:
            if STOP_EVENT.is_set():
                kill_process(proc)
                err_text = "用户中断"
                interrupted = True
                break
            if proc.poll() is not None:
                break
            if activity.age() > STAGNATION_WATCHDOG_SEC:
                kill_process(proc)
                err_text = "解码停滞超时"
                break
            STOP_EVENT.wait(0.1)
    finally:
        try:
            proc.stderr.close()
        except Exception:
            pass
        stderr_thread.join(timeout=2)
        if not err_text and not interrupted:
            err_text = ''.join(stderr_lines)

    if interrupted:
        return err_text, False, True
    if err_text == "解码停滞超时":
        return err_text, False, False
    if proc.returncode != 0:
        if is_hw_init_fatal(err_text):
            return err_text[:1000], True, False
        return err_text[:1000], False, False
    return None, False, False


def decode_with_fallback(file_path, stream_info, hw_methods, gpu_idx):
    codec = stream_info.codec_name
    spec = stream_info.stream_spec
    issues = []
    warnings = []
    tried_gpu = False
    is_audio = (stream_info.codec_type == 'audio')

    if is_audio:
        cpu_cmd = build_decode_command(file_path, stream_info, 'cpu')
        debug(2, "[decode] %s 音频流 CPU 解码 (codec=%s)", spec, codec)
        debug(2, "[cmd] %s", shlex.join(cpu_cmd))
        err, _, interrupted = run_decode(cpu_cmd)
        if interrupted:
            return issues, warnings, True
        if err:
            issues.append(f"{spec}:[音频解码错误]{err[:1000]}")
        return issues, warnings, False

    if 'cuda' in hw_methods and codec in NVDEC_CODECS:
        tried_gpu = True
        debug(2, "[decode] %s 尝试 NVDEC (codec=%s)", spec, codec)
        cmd = build_decode_command(file_path, stream_info, 'cuda', gpu_idx)
        debug(2, "[cmd] %s", shlex.join(cmd))
        err, hw_fail, interrupted = run_decode(cmd)
        if interrupted:
            return issues, warnings, True
        if err is None:
            return issues, warnings, False
        debug(2, "[decode] %s NVDEC 结果: err=%s hw_fail=%s", spec,
              err[:200] if err else 'OK', hw_fail)
        if hw_fail:
            warnings.append(
                f"{spec}: NVDEC硬件初始化失败({err[:100]})"
            )
        else:
            # 降级为 warning：CPU 软解成功后不应因 GPU 兼容性问题判 FAIL
            warnings.append(
                f"{spec}: [NVDEC解码失败，降至CPU] {err[:200]}"
            )

    if 'qsv' in hw_methods and codec in QSV_CODECS:
        tried_gpu = True
        debug(2, "[decode] %s 尝试 QSV (codec=%s)", spec, codec)
        cmd = build_decode_command(file_path, stream_info, 'qsv')
        debug(2, "[cmd] %s", shlex.join(cmd))
        err, hw_fail, interrupted = run_decode(cmd)
        if interrupted:
            return issues, warnings, True
        if err is None:
            return issues, warnings, False
        debug(2, "[decode] %s QSV 结果: err=%s hw_fail=%s", spec,
              err[:200] if err else 'OK', hw_fail)
        if hw_fail:
            warnings.append(
                f"{spec}: QSV硬件初始化失败({err[:100]})"
            )
        else:
            # 降级为 warning：CPU 软解成功后不应因 GPU 兼容性问题判 FAIL
            warnings.append(
                f"{spec}: [QSV解码失败，降至CPU] {err[:200]}"
            )

    cpu_cmd = build_decode_command(file_path, stream_info, 'cpu')
    debug(2, "[decode] %s 降级 CPU", spec)
    debug(2, "[cmd] %s", shlex.join(cpu_cmd))
    err, _, interrupted = run_decode(cpu_cmd)

    if interrupted:
        return issues, warnings, True
    if err:
        issues.append(f"{spec}:[CPU解码错误]{err[:1000]}")
    elif tried_gpu and not issues:
        warnings.append(
            f"{spec}: GPU加速不可用但CPU解码通过"
        )

    return issues, warnings, False
