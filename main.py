# main.py — entry point

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import signal
import shutil
import argparse

from config import (
    SUPPORTED_EXTENSIONS, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, AUDIO_EXTENSIONS,
    EST_CPU_MEM_PER_DECODE_MB,
    STOP_EVENT, debug, debug_set, set_fast_packet_scan_seconds,
)
from db import CheckpointManager
from display import ProgressDisplay
from limiter import ConcurrencyLimiter
from hardware import (
    get_best_gpu, get_system_free_memory_mb, check_hwaccel_methods,
    probe_disk_latency, determine_max_workers,
)
from check import collect_files, run_checks


def signal_handler(signum, frame):
    print(f"\n[收到终止信号 {signum}] 安全中止检查...")
    STOP_EVENT.set()


def _build_db_url(args):
    if args.db_url:
        return args.db_url, {}
    return 'sqlite:///' + os.path.abspath(args.checkpoint), {}


def final_summary(data, total_files):
    fail = sum(1 for v in data.values() if v['status'] == 'failure')
    warn = sum(1 for v in data.values() if v['status'] == 'warning')
    succ = sum(1 for v in data.values() if v['status'] == 'success')
    unprocessed = total_files - len(data)
    print(f"\n========== 媒体校验汇总 ==========")
    print(f"总计文件: {total_files}")
    print(f"[OK] 完好通过: {succ}")
    print(f"[!!] 轻微警告: {warn}")
    print(f"[XX] 损坏失效: {fail}")
    if unprocessed > 0:
        print(f"[--] 未校验: {unprocessed}")
    if fail or warn:
        print("\n---------- 异常文件明细 ----------")
        for path, info in data.items():
            if info['status'] not in ('failure', 'warning'):
                continue
            print(f"\n文件: {path}")
            for err in info['issues']:
                print(f"  错误: {err}")
            for war in info['warnings']:
                print(f"  警告: {war}")


def main():
    STOP_EVENT.clear()
    cp = None
    probe_cleanup = None

    if os.name != 'nt':
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    parser = argparse.ArgumentParser(description="媒体完整性校验工具")
    parser.add_argument('directory', help="待扫描根目录")
    parser.add_argument('--ext', nargs='+', help="指定后缀名，如 mp4 mkv")
    parser.add_argument('--skip-decode', action='store_true',
                        help="跳过解码校验，仅帧扫描")
    parser.add_argument('--cpu-only', action='store_true',
                        help="强制仅CPU解码")
    parser.add_argument('--max-workers', type=int, help="手动指定并发任务数")
    parser.add_argument('--checkpoint', default='./media_checkpoint.db',
                        help="断点数据库路径")
    parser.add_argument('--recheck', action='store_true',
                        help="强制全部重新校验，清空断点")
    parser.add_argument('--full-ts-scan', action='store_true',
                        help="禁用TS流快速扫描")
    parser.add_argument('--retry-failures', action='store_true',
                        help="重新校验上次标记为failed的文件")
    parser.add_argument('--db-url', default=None,
                        metavar='URL',
                        help="SQLAlchemy 数据库连接字符串"
                             "（如 postgresql://user:pass@host/db?sslmode=require）")
    parser.add_argument('--debug', type=int, default=0, choices=[0,1,2,3],
                        help="调试日志级别 (0=关 1=流程 2=详情+命令 3=全量)")
    parser.add_argument('--type', default='all',
                        choices=['all', 'video', 'audio', 'image'],
                        help="检测类型 (默认 all)")
    args = parser.parse_args()

    debug_set(args.debug)

    if args.full_ts_scan:
        set_fast_packet_scan_seconds(0)

    try:
        for tool in ('ffprobe', 'ffmpeg'):
            if not shutil.which(tool):
                sys.exit(
                    f"错误：系统未找到 {tool}，请先安装FFmpeg并配置环境变量"
                )

        if args.ext:
            extensions = {
                (e if e.startswith('.') else f'.{e}').lower()
                for e in args.ext
            }
        else:
            extensions = SUPPORTED_EXTENSIONS

        hw_methods = set() if args.cpu_only else check_hwaccel_methods()
        gpu_idx, gpu_free = 0, 0
        has_nv = not args.cpu_only and 'cuda' in hw_methods
        if has_nv:
            gpu_idx, gpu_free = get_best_gpu()
        has_qsv = 'qsv' in hw_methods

        all_files = collect_files(args.directory, extensions)
        if args.type != 'all':
            type_exts = {'image': IMAGE_EXTENSIONS,
                         'video': VIDEO_EXTENSIONS,
                         'audio': AUDIO_EXTENSIONS}[args.type]
            all_files = [f for f in all_files
                         if os.path.splitext(f)[1].lower() in type_exts]
        if not all_files:
            print("未扫描到符合后缀的媒体文件")
            return

        db_url, ekw = _build_db_url(args)
        cp = CheckpointManager(db_url,
                               retry_failures=args.retry_failures,
                               engine_kwargs=ekw)
        if args.recheck:
            cp.clear()
            done_paths = set()
        else:
            done_paths = cp.get_done_paths()

        files_to_check = [f for f in all_files if f not in done_paths]
        skipped = len(all_files) - len(files_to_check)
        debug(1, "[checkpoint] 总 %d 文件, 跳过 %d, 待扫 %d",
              len(all_files), skipped, len(files_to_check))
        print(
            f"跳过已完成: {skipped} 个 | "
            f"待校验文件: {len(files_to_check)}"
        )
        if not files_to_check:
            final_summary(cp.get_all(), len(all_files))
            return

        sys_free = get_system_free_memory_mb()
        disk_latency, probe_cleanup = probe_disk_latency(args.directory, count=3)
        raw_workers = determine_max_workers(
            gpu_free, sys_free, has_nv, has_qsv,
            args.cpu_only, args.max_workers,
            disk_latency_ms=disk_latency
        )
        debug(1, "[workers] 初始并发: %d (NV=%s QSV=%s CPU=%s) disk=%.1fms",
              raw_workers, has_nv, has_qsv, args.cpu_only,
              disk_latency or 0)
        method = "固定1MB探针" if disk_latency is not None else "无法探测"
        print(f"磁盘延迟: {(disk_latency or 0):.1f}ms ({method}) | 初始并发任务数: {raw_workers}")

        max_cpu = max(2, min(raw_workers, sys_free // EST_CPU_MEM_PER_DECODE_MB)) if sys_free > 0 else raw_workers
        dl = disk_latency or 999
        if dl < 5:
            iops_limit = raw_workers
        elif dl < 20:
            iops_limit = max(2, raw_workers // 3)
        elif dl < 100:
            iops_limit = 2
        else:
            iops_limit = 1
        debug(1, "[workers] CPU上限=%d IOPS上限=%d (disk %.1fms)", max_cpu, iops_limit, dl)
        limiter = ConcurrencyLimiter(
            initial_cap=max(2, raw_workers // 2),
            max_cap=raw_workers,
            max_cpu_tasks=max_cpu,
            iops_limit=iops_limit,
            probe_interval=30
        )

        display = ProgressDisplay(len(files_to_check))
        display.add("开始媒体完整性校验")
        display.draw()

        run_checks(
            files_to_check, hw_methods, not args.skip_decode,
            raw_workers, cp, display, gpu_idx, limiter
        )

        final_summary(cp.get_all(), len(all_files))

    except KeyboardInterrupt:
        print("\n校验已中止，已完成结果自动保存")
    finally:
        if probe_cleanup:
            probe_cleanup()
        if cp is not None:
            cp.close()


if __name__ == '__main__':
    main()
