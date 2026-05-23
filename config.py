# config.py — 全局配置常量
#
# 本文件包含所有可调参数。需要修改行为时优先修改此文件。
# 命令行参数（如 --max-workers）会覆盖此处的默认值。

import threading

# -----------------------------------------------------------------
# 文件扩展名过滤（需要增加/移除格式时修改这些集合）
# -----------------------------------------------------------------

# 默认内置支持的所有扩展名。--ext 参数会覆盖此默认值
SUPPORTED_EXTENSIONS = {
    '.mp4', '.mkv', '.avi', '.mov', '.flv',
    '.wmv', '.webm', '.m4v', '.mpeg',
    '.mpg', '.ts', '.m2ts',
    '.mp3', '.aac', '.flac', '.wav', '.ogg',
    '.opus', '.m4a', '.wma', '.wavpack', '.ape',
    '.jpg', '.jpeg', '.png', '.gif', '.bmp',
    '.webp', '.tiff', '.tif', '.avif', '.heic',
}

# 流媒体格式：容器 duration 不可信，不做截断检测，只扫头部
STREAMING_EXTENSIONS = {'.ts', '.m2ts', '.m2t'}

# --type 参数使用的分类（添加新格式时同步更新）
IMAGE_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.bmp',
    '.webp', '.tiff', '.tif', '.avif', '.heic',
}

VIDEO_EXTENSIONS = {
    '.mp4', '.mkv', '.avi', '.mov', '.flv',
    '.wmv', '.webm', '.m4v', '.mpeg',
    '.mpg', '.ts', '.m2ts',
}

AUDIO_EXTENSIONS = {
    '.mp3', '.aac', '.flac', '.wav', '.ogg',
    '.opus', '.m4a', '.wma', '.wavpack', '.ape',
}

# -----------------------------------------------------------------
# Packet 扫描 — 帧完整性检测阈值
# -----------------------------------------------------------------

# 视频帧间隔超过此值（毫秒）标记为疑似缺帧
GAP_THRESHOLD_DURATION_MS = 500

# 音频帧间隔阈值（比视频宽松，因为音频帧时长更长）
AUDIO_GAP_THRESHOLD_DURATION_MS = 2000

# 容器记录的时长与扫描末尾 PTS 的差值超过此值（秒）标记为疑似截断
TRUNCATION_THRESHOLD_SECONDS = 2.0

# 短文件（<30s）截断阈值更宽松，因为 ffprobe 时长估算误差相对更大
SHORT_FILE_TRUNCATION_THRESHOLD_SECONDS = 5.0
SHORT_FILE_DURATION_SECONDS = 30.0

# PTS/DTS 回退超过此值（秒）才报告错误（小于 1s 的回退可能是 B 帧重排，属正常）
PTS_BACKWARD_THRESHOLD_SEC = 1.0

# 视频 VFR 模式下，gap 超过此值才标记（避免手机录像的天然大 gap 误报）
VFR_GAP_THRESHOLD_SEC = 5.0

# -----------------------------------------------------------------
# 超时与保护
# -----------------------------------------------------------------

# TS/M2TS 文件只扫描前 N 秒（--full-ts-scan 可禁用以扫描全量）
FAST_PACKET_SCAN_SECONDS = 300


def set_fast_packet_scan_seconds(value):
    global FAST_PACKET_SCAN_SECONDS
    FAST_PACKET_SCAN_SECONDS = value

# ffprobe/ffmpeg stdout/stderr 无输出超过 N 秒判定为卡死
STAGNATION_WATCHDOG_SEC = 10

# -----------------------------------------------------------------
# 并发与资源估算（修改前请确认硬件实际规格）
# -----------------------------------------------------------------

# 并发硬上限（磁盘探针和系统内存会进一步削减）
MAX_IO_WORKERS = 16

# 每个流最多扫描的 packet 数（单流，防止单个超大流扫不完）
MAX_PACKET_SCAN_PER_STREAM = 500_000

# 所有流合计最大 packet 数（全局安全阀）
MAX_PACKET_SCAN_GLOBAL = 5_000_000

# 单流报告 issues 上限（超出后截断，防止 1 万条错误刷屏）
MAX_ISSUES_PER_STREAM = 100

# 单任务内存估算值（MB）：用于从系统空闲内存推算最大并发数
# 如果你的文件普遍 4K/8K 或你的 CPU 解码内存占用不同，请调整
EST_GPU_MEM_PER_DECODE_MB = 1024   # NVDEC 每个解码任务约占用显存
EST_QSV_MEM_PER_DECODE_MB = 1024   # QSV 类似
EST_CPU_MEM_PER_DECODE_MB = 2048   # CPU 软解每个任务约占用内存

# GPU 空闲显存低于此值（MB）时不启动新 NVDEC 任务
MIN_GPU_FREE_MB = 512

# -----------------------------------------------------------------
# 数据库
# -----------------------------------------------------------------

# 每攒 N 条记录提交一次数据库事务
BATCH_COMMIT_SIZE = 100

# -----------------------------------------------------------------
# 进程间通信缓冲
# -----------------------------------------------------------------

# Linux 管道缓冲区大小（1MB），仅在 Python 3.10+ 生效
PIPE_BUFFER_SIZE = 1024 * 1024

# ffprobe CSV 单行最大长度（超过视为异常数据丢弃）
MAX_LINE_LENGTH = 100 * 1024

# -----------------------------------------------------------------
# 硬件编解码器支持列表（驱动/FFmpeg 版本不同时可能需要调整）
# -----------------------------------------------------------------

NVDEC_CODECS = {'h264', 'hevc', 'mpeg2video', 'vc1', 'vp8', 'vp9', 'av1'}
QSV_CODECS = {'h264', 'hevc', 'mpeg2video', 'vc1', 'vp8', 'vp9', 'av1'}

# -----------------------------------------------------------------
# 全局状态（不需要修改）
# -----------------------------------------------------------------

# 全局停止信号：Ctrl+C 或 SIGTERM 时置位，所有 worker 收到后尽快退出
STOP_EVENT = threading.Event()

# 调试日志级别：0=关 1=流程 2=详情+命令行 3=全量
DEBUG_LEVEL = 0


def debug(level, fmt, *args):
    """向 stderr 输出带毫秒时间戳的调试日志。level 仅在 >= DEBUG_LEVEL 时输出。"""
    import time
    import sys
    if DEBUG_LEVEL >= level:
        ts = time.strftime("[%H:%M:%S")
        if args:
            try:
                msg = fmt % args
            except (TypeError, ValueError):
                msg = str(fmt)
        else:
            msg = str(fmt)
        print(f"{ts}.{int(time.time()*1000)%1000:03d}] {msg}",
              file=sys.stderr, flush=True)


def debug_set(level):
    """设置全局调试级别（由 --debug 参数调用）。"""
    global DEBUG_LEVEL
    DEBUG_LEVEL = level
