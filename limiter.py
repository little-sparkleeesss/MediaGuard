# limiter.py — 动态并发控制 + IO 活性检测
#
# ConcurrencyLimiter: TCP 风格 AIMD 自适应并发
#   三个独立上限：全局 cap、CPU 任务子池、小文件 IOPS 子池
#   通过吞吐率（files/s）动态探测最佳并发数，30s 一轮评估
#
# PacketActivity: IO 活性跟踪（stderr/stdout 是否仍在输出）
#   用于 ffprobe/ffmpeg 停滞看门狗

import time
import sys
import threading

from config import STOP_EVENT, debug


class PacketActivity:
    """跟踪最后一次 IO 活动的时间戳，用于检测进程是否卡死。"""
    __slots__ = ('last_activity', 'lock')

    def __init__(self):
        self.last_activity = time.monotonic()
        self.lock = threading.Lock()

    def touch(self):
        """标记活动（每次 stderr/stdout 有数据时调用）。"""
        with self.lock:
            self.last_activity = time.monotonic()

    def age(self):
        """返回距离上次活动已经过去了多少秒。"""
        with self.lock:
            return time.monotonic() - self.last_activity


class ConcurrencyLimiter:
    """TCP AIMD 自适应并发限制器。

    对外接口：
        acquire(cpu_heavy, small_file) -> 阻塞直到获得执行槽位
        release(cpu_heavy, small_file) -> 释放槽位并通知等待者

    三种约束（AND 逻辑，全部满足才放行）：
        active < cap           总并发上限（AIMD 动态调整）
        active_cpu < max_cpu   CPU 任务上限（基于系统空闲内存）
        active_io < io_limit   小文件 IOPS 上限（基于磁盘延迟）
    """

    def __init__(self, initial_cap, max_cap, max_cpu_tasks=0, iops_limit=0,
                 probe_interval=30):
        self._condition = threading.Condition()
        self._cap = initial_cap              # 当前允许的最大并发数
        self._max_cap = max_cap              # 物理硬上限
        self._active = 0                     # 当前活跃任务总数
        self._active_cpu = 0                 # CPU 密集型任务数（音频/图片）
        self._active_io = 0                  # 小文件 IO 任务数
        self._max_cpu = max_cpu_tasks if max_cpu_tasks > 0 else max_cap
        self._io_limit = iops_limit if iops_limit > 0 else max_cap

        # AIMD 状态
        self._release_count = 0              # 累计完成数
        self._last_releases = 0              # 上一轮完成数
        self._probe_interval = probe_interval
        self._best_rate_per_worker = 0.0     # 历史最佳每 worker 吞吐
        self._cooldown = 0                   # 冷却期（调整后锁 N 轮不再动作）

        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor.start()

    def _monitor_loop(self):
        """后台监控线程：每 probe_interval 秒评估吞吐并调整 cap。"""
        while not STOP_EVENT.is_set():
            STOP_EVENT.wait(self._probe_interval)
            if STOP_EVENT.is_set():
                break

            with self._condition:
                completed = self._release_count - self._last_releases
                self._last_releases = self._release_count
                old_cap = self._cap
                active_now = self._active
                active_cpu = self._active_cpu
                active_io = self._active_io

            raw_rate = completed / self._probe_interval
            avg_active = max(1, float(active_now))
            per_worker = raw_rate / avg_active if avg_active > 0 else raw_rate

            if completed == 0 and active_now == 0:
                self._best_rate_per_worker = self._best_rate_per_worker * 0.92

            if self._cooldown > 0:
                self._cooldown -= 1
                new_cap = old_cap
            elif completed == 0 and active_now > 0 and old_cap < self._max_cap:
                # 有大任务在跑但本窗口无产出 → 未饱和，尝试增加并发
                new_cap = old_cap + 1
                self._cooldown = 2
            elif completed == 0 and active_now == 0 and old_cap > 1:
                # 全部空闲且无产出 → 队列空了，可以降
                new_cap = old_cap - 1
                self._cooldown = 2
            elif per_worker > self._best_rate_per_worker * 1.2:
                # 吞吐明显改善 → 加性增长
                self._best_rate_per_worker = max(self._best_rate_per_worker, per_worker)
                new_cap = min(self._max_cap, old_cap + 1)
                self._cooldown = 2
            elif per_worker < self._best_rate_per_worker * 0.6 and old_cap > 1 and completed > 0:
                # 吞吐明显恶化且有完成记录 → 减 1（大任务进行中就不减）
                new_cap = max(1, old_cap - 1)
                self._cooldown = 2
            else:
                new_cap = old_cap

            if new_cap != old_cap:
                with self._condition:
                    self._cap = new_cap
                    self._condition.notify_all()
                direction = "↑" if new_cap > old_cap else "↓"
                msg = (f"[并发{direction}] {old_cap} → {new_cap} "
                       f"(rate={raw_rate:.1f}/s pw={per_worker:.2f} "
                       f"active={active_now} cpu={active_cpu}/{self._max_cpu} "
                       f"io={active_io}/{self._io_limit})")
                debug(1, "%s", msg)
                print(msg, file=sys.stderr, flush=True)
            elif new_cap == old_cap:
                debug(2, "[并发=] cap=%d/%d active=%d cpu=%d/%d io=%d/%d rate=%.1f/s pw=%.2f",
                      old_cap, self._max_cap, active_now,
                      active_cpu, self._max_cpu,
                      active_io, self._io_limit, raw_rate, per_worker)

    def acquire(self, cpu_heavy=False, small_file=False):
        """获取执行槽位。可能阻塞直到有空间。"""
        with self._condition:
            while not STOP_EVENT.is_set():
                if self._active >= self._cap:
                    self._condition.wait(timeout=1)
                    continue
                if cpu_heavy and self._active_cpu >= self._max_cpu:
                    self._condition.wait(timeout=1)
                    continue
                if small_file and self._active_io >= self._io_limit:
                    self._condition.wait(timeout=1)
                    continue
                break
            self._active += 1
            if cpu_heavy:
                self._active_cpu += 1
            if small_file:
                self._active_io += 1

    def release(self, cpu_heavy=False, small_file=False):
        """释放执行槽位。"""
        with self._condition:
            self._active -= 1
            if cpu_heavy:
                self._active_cpu -= 1
            if small_file:
                self._active_io -= 1
            self._release_count += 1
            self._condition.notify()

    @property
    def max_cpu(self):
        return self._max_cpu
