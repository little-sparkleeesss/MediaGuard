# MediaGuard

批量检测音视频及图片文件完整性的命令行工具。结合 ffprobe 帧级扫描与 ffmpeg 解码校验，支持硬件加速降级、断点续传、动态并发调度。

## 功能特性

- **三级检测**：Packet 帧扫描（缺帧/截断/DTS-PTS 回退） + ffmpeg 解码校验（NVDEC → QSV → CPU 逐级降级）
- **多格式覆盖**：MP4 / MKV / AVI / MOV / TS / WebM 等视频；MP3 / FLAC / AAC / WAV 等音频；JPG / PNG / WebP / HEIC 等图片
- **断点续传**：SQLite 持久化进度，中断后自动跳过已完成文件（支持 `--recheck` / `--retry-failures`）
- **硬件感知**：自动探测 NVIDIA GPU 显存、系统空闲内存、磁盘 IO 延迟，动态计算最优并发数
- **AIMD 自适应并发**：TCP 风格动态调整并发上限，避免 I/O 过载
- **ANSI 进度条**：实时显示总进度、文件状态、自动换行长文件名（CJK 宽度适配）

## 依赖

- Python 3.9+
- [FFmpeg](https://ffmpeg.org/) / ffprobe（需在 PATH 中可用）
- SQLAlchemy（可选 psycopg2 / psutil）

```bash
pip install sqlalchemy psutil
```

可选 PostgreSQL 支持：
```bash
pip install psycopg2-binary
```

## 安装

```bash
git clone https://github.com/yourname/mediaguard.git
cd mediaguard
pip install -r requirements.txt
```

## 快速开始

```bash
# 检测目录下所有媒体文件
python main.py /path/to/media

# 仅检测视频
python main.py /path/to/media --type video

# 仅检测图片和音频
python main.py /path/to/media --type image --type audio

# 指定扩展名
python main.py /path/to/media --ext mp4 mkv avi

# 强制全部重新校验（清空断点）
python main.py /path/to/media --recheck

# 重新校验上次标记为失败的文件
python main.py /path/to/media --retry-failures

# 纯 CPU 模式（禁用硬件加速）
python main.py /path/to/media --cpu-only

# 手动指定并发数
python main.py /path/to/media --max-workers 4

# 调试模式（级别 1-3）
python main.py /path/to/media --debug 2

# 使用外部 PostgreSQL 保存进度
python main.py /path/to/media --db-url "postgresql://user:pass@host/db"
```

## 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `directory` | 待扫描的根目录（必填） | — |
| `--ext` | 指定后缀名，如 `mp4 mkv` | 全部内置格式 |
| `--type` | 检测类型：`all` / `video` / `audio` / `image` | `all` |
| `--skip-decode` | 跳过解码校验，仅帧扫描 | 否 |
| `--cpu-only` | 强制仅用 CPU 解码 | 否 |
| `--max-workers` | 手动指定最大并发任务数 | 自动探测 |
| `--checkpoint` | 断点数据库文件路径 | `./media_checkpoint.db` |
| `--recheck` | 强制全部重新校验，清空断点 | 否 |
| `--retry-failures` | 重新校验上次 failed 的文件 | 否 |
| `--full-ts-scan` | 禁用 TS 流快速扫描（默认仅扫前 5 分钟） | 否 |
| `--db-url` | PostgreSQL 连接字符串 | （使用 SQLite） |
| `--debug` | 调试日志级别 0-3 | `0` |

## 检测项说明

### 帧扫描（Packet Scan）

- **DTS/PTS 回退**：时间戳倒退超过阈值，指示帧乱序或损坏
- **疑似缺帧**：PTS 间隔超出预期（VFR 视频使用更宽松的阈值）
- **疑似截断**：容器记录的时长与末尾 PTS 差距过大
- **空流**：声明了流但没有任何 Packet 数据

### 解码校验（Decode Verification）

- 逐流使用 ffmpeg 解码到 `/dev/null`
- 硬件加速链路：**NVDEC** → **QSV** → **CPU**（任一成功即通过）
- 音频流直接使用 CPU 解码（硬件加速对音频无收益）
- 已检测到结构性损坏时自动跳解码，避免 ffmpeg 卡死

### 图片检测

- 使用 ffmpeg 完整解码图片，60 秒超时

## 输出结果

```
========== 媒体校验汇总 ==========
总计文件: 1523
[OK] 完好通过: 1487
[!!] 轻微警告: 21
[XX] 损坏失效: 12
[--] 未校验: 3

---------- 异常文件明细 ----------

文件: /path/to/broken.mp4
  错误: [疑似截断] v:0 容器120.500s 实际87.300s 差33.200s
  警告: [NVDEC解码失败，降至CPU] ...
```

## 项目结构

```
mediaguard/
├── main.py       # 入口，参数解析，流程编排
├── config.py     # 全局配置常量，可调参数
├── check.py      # 文件校验逻辑，文件收集，批量执行
├── media.py      # 流信息模型，Packet 增量分析，ffprobe 扫描
├── decode.py     # ffmpeg 解码与硬件加速降级
├── hardware.py   # GPU/内存/磁盘探针，并发数计算
├── limiter.py    # AIMD 自适应并发控制器
├── display.py    # ANSI 终端进度条（CJK 宽度适配）
└── db.py         # SQLAlchemy 断点管理器（SQLite/PostgreSQL）
```

## 配置调优

`config.py` 中包含所有可调参数，常用项：

| 常量 | 说明 | 默认值 |
|------|------|--------|
| `BATCH_COMMIT_SIZE` | DB 批量提交大小 | 100 |
| `FAST_PACKET_SCAN_SECONDS` | TS流快速扫描秒数 | 300 |
| `TRUNCATION_THRESHOLD_SECONDS` | 截断检测阈值 | 2.0 |
| `GAP_THRESHOLD_DURATION_MS` | 视频缺帧阈值 | 500 |
| `EST_CPU_MEM_PER_DECODE_MB` | 每任务内存估耗 | 2048 |
| `MAX_IO_WORKERS` | 并发硬上限 | 16 |
| `STAGNATION_WATCHDOG_SEC` | 进程停滞超时 | 10 |

## License

MIT
