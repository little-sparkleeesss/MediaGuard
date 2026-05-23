# hardware.py — GPU、内存、磁盘探针 + 并发计算
#
# 启动时自动检测硬件环境，用于决策最优并发数。

import os
import re
import sys
import time
import subprocess

from config import (
    EST_GPU_MEM_PER_DECODE_MB, EST_QSV_MEM_PER_DECODE_MB,
    EST_CPU_MEM_PER_DECODE_MB, MIN_GPU_FREE_MB, MAX_IO_WORKERS,
    debug,
)


def get_best_gpu():
    """获取显存最多的 NVIDIA GPU 编号和剩余显存(MB)。无 GPU 返回 (0, 0)。"""
    try:
        proc = subprocess.run(
            ['nvidia-smi', '--query-gpu=index,memory.free',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=10
        )
        best_idx, best_mem = 0, 0
        # 正则匹配 "  0, 8192  " 格式（兼容不同版本的空白字符差异）
        pattern = re.compile(r'^\s*(\d+)\s*,\s*(\d+)\s*$')
        for line in proc.stdout.strip().splitlines():
            match = pattern.match(line)
            if not match:
                continue
            idx = int(match.group(1))
            mem = int(match.group(2))
            if mem > best_mem:
                best_mem, best_idx = mem, idx
        return best_idx, best_mem
    except Exception:
        return 0, 0


def get_system_free_memory_mb():
    """获取系统空闲内存(MB)。优先使用 psutil，回退读取 /proc/meminfo。"""
    try:
        import psutil
        return psutil.virtual_memory().available // (1024 * 1024)
    except Exception:
        pass
    if os.path.exists('/proc/meminfo'):
        try:
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    if line.startswith('MemAvailable:'):
                        return int(line.split()[1]) // 1024
        except Exception:
            pass
    return 0


def check_hwaccel_methods():
    """返回可用的硬件加速方法集合（如 {'cuda', 'qsv'}）。
    QSV 会做真实硬件探针：编译支持 ≠ 硬件存在。"""
    try:
        proc = subprocess.run(
            ['ffmpeg', '-hwaccels'],
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=10
        )
        methods = {line.strip().lower() for line in proc.stdout.splitlines()
                   if line.strip() and not line.startswith('Hardware')}

        # QSV 不仅检查 ffmpeg 编译支持，还要跑一次真实编解码确认硬件可用
        if 'qsv' in methods:
            if not _probe_qsv():
                methods.discard('qsv')

        return methods
    except Exception:
        return set()


def _probe_qsv():
    """用一小段黑屏视频试编解码来确认 QSV 硬件是否真实可用。"""
    try:
        proc = subprocess.run(
            ['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error',
             '-init_hw_device', 'qsv=hw',
             '-f', 'lavfi', '-i', 'color=c=black:s=32x32:d=0.1',
             '-c:v', 'h264_qsv', '-f', 'null', '-'],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=15
        )
        return proc.returncode == 0
    except Exception:
        return False


def probe_disk_latency(root_dir, count=3):
    """在目标目录创建 3 个 1MB 固定大小探针文件，做随机 seek+read 测速。
    
    返回:
        (latency_ms, cleanup_fn) — 延迟（毫秒）和清理函数
        (None, None) — 目录不可写，放弃评估
    
    探针文件为隐藏文件 .media_check_probe_<tag>_N.tmp，退出时由 cleanup_fn 删除。
    """
    import random as _random
    import uuid as _uuid

    probe_files = []
    probe_size = 1024 * 1024
    data = bytes([_random.randint(0, 255) for _ in range(probe_size)])

    tag = _uuid.uuid4().hex[:8]
    created = 0
    for i in range(count):
        path = os.path.join(root_dir, f".media_check_probe_{tag}_{i}.tmp")
        try:
            with open(path, 'wb') as f:
                f.write(data)
            probe_files.append(path)
            created += 1
            debug(3, "[probe] 创建 %s (1MB)", path)
        except OSError as e:
            debug(3, "[probe] 创建失败 %s: %s", path, e)

    if not probe_files:
        print("[警告] 磁盘探针文件创建失败 (0/%d)，跳过延迟评估" % count,
              file=sys.stderr, flush=True)
        return None, None

    print("[磁盘探测] 在 %s 创建 %d 个固定大小临时文件，正在测速..." % (root_dir, created),
          file=sys.stderr, flush=True)

    chunk = 65536
    latencies = []
    for fp in probe_files:
        for _ in range(3):
            offset = _random.randint(0, probe_size - chunk)
            t0 = time.monotonic()
            with open(fp, 'rb') as f:
                f.seek(offset)
                f.read(chunk)
            latencies.append((time.monotonic() - t0) * 1000)
        debug(3, "[probe] 已测速 %s", fp)

    def cleanup():
        """只删除文件名符合本次 tag 的探针，防止误删。"""
        prefix = f".media_check_probe_{tag}_"
        for fp in probe_files:
            if not os.path.basename(fp).startswith(prefix):
                continue
            try:
                os.remove(fp)
                debug(3, "[probe] cleanup: 已删除 %s", fp)
            except OSError as e:
                debug(3, "[probe] cleanup: 删除失败 %s: %s", fp, e)

    avg_latency = sum(latencies) / len(latencies)
    debug(3, "[probe] 平均延迟 %.1fms (%d 次采样)", avg_latency, len(latencies))
    return avg_latency, cleanup


def _latency_to_factor(latency_ms):
    """将磁盘延迟（ms）映射为并发削减系数。
    
    延迟区间     系数   典型介质
    <5ms        1.0    SSD/NVMe
    5-20ms      0.7    HDD
    20-100ms    0.4    NAS/NFS
    >100ms      0.2    远程/S3
    """
    if latency_ms < 5:
        return 1.0
    elif latency_ms < 20:
        return 0.7
    elif latency_ms < 100:
        return 0.4
    else:
        return 0.2


def determine_max_workers(gpu_free, sys_free, has_nv, has_qsv,
                          force_cpu, manual=None, disk_latency_ms=None):
    """综合 GPU 显存、系统内存、磁盘延迟计算最优并发数。
    
    --max-workers 传入手动值时直接返回，跳过自动计算。
    """
    if manual is not None:
        return max(1, manual)

    cpu_count = os.cpu_count() or 2

    # 纯 CPU 模式：仅受内存约束
    if force_cpu:
        cpu_workers = (
            max(1, sys_free // EST_CPU_MEM_PER_DECODE_MB)
            if sys_free > 0 else cpu_count
        )
        return min(cpu_workers, MAX_IO_WORKERS, cpu_count)

    # GPU 模式：NVDEC + QSV 分别根据显存/内存计算
    nv, qsv = 0, 0
    if has_nv and gpu_free >= MIN_GPU_FREE_MB:
        nv = min(8, gpu_free // EST_GPU_MEM_PER_DECODE_MB)
    if has_qsv:
        qsv = (
            min(6, sys_free // EST_QSV_MEM_PER_DECODE_MB)
            if sys_free > 0 else 1
        )
    workers = max(1, nv + qsv + 2)           # +2 为 CPU 预留
    workers = min(workers, MAX_IO_WORKERS, cpu_count)

    # 磁盘延迟附加削减
    if disk_latency_ms is not None:
        factor = _latency_to_factor(disk_latency_ms)
        workers = max(1, int(workers * factor))
        debug(2, "[disk] 延迟=%.1fms factor=%.1f workers=%d",
              disk_latency_ms, factor, workers)

    return workers
