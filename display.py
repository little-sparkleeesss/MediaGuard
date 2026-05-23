# display.py — 终端进度条（ANSI 转义 + CJK 字符宽度适配）

import os
import sys
import time
import shutil
import threading
import unicodedata
from collections import deque

from config import STOP_EVENT


def truncate_by_display_width(text, max_width):
    """按等宽终端的视觉宽度截断字符串。
    CJK 全角字符占 2 列，ASCII 占 1 列。"""
    width = 0
    for i, char in enumerate(text):
        width += 2 if unicodedata.east_asian_width(char) in ('F', 'W') else 1
        if width > max_width:
            return text[:i]
    return text


def wrap_by_display_width(text, max_width):
    """按终端宽度将字符串拆成多行。
    用于 ANSI 进度条模式下防止物理换行破坏光标上移计数。"""
    lines = []
    start = 0
    width = 0
    for i, char in enumerate(text):
        char_width = 2 if unicodedata.east_asian_width(char) in ('F', 'W') else 1
        if width + char_width > max_width:
            lines.append(text[start:i])
            start = i
            width = 0
        width += char_width
    if start < len(text):
        lines.append(text[start:])
    return lines


class ProgressDisplay:
    """终端进度条。
    
    特性：
        - 每隔 0.2s 刷新一次，避免高频终端 IO
        - 中断后立即冻结刷新，防止 print() 输出被 ANSI 覆盖
        - 自动换行超长文件名（按终端列宽 + CJK 宽度计算）
        - stdout 非 TTY 时回退为普通 print（适合日志重定向）
    """

    def __init__(self, total):
        self.total = total
        self.completed = 0
        self.logs = deque(maxlen=15)       # 最近 15 条日志
        self.lock = threading.Lock()
        self.enable_ansi = sys.stdout.isatty()
        self.last_draw = 0

        # Windows 下尝试启用 ANSI 转义支持（Win10 1607+）
        if os.name == 'nt' and self.enable_ansi:
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.GetStdHandle(-11)
                mode = ctypes.c_uint()
                kernel32.GetConsoleMode(handle, ctypes.byref(mode))
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
            except Exception:
                pass

    def add(self, msg):
        with self.lock:
            ts = time.strftime("[%H:%M:%S]")
            self.logs.append(f"{ts} {msg}")

    def update(self, n):
        with self.lock:
            self.completed = n

    def draw(self):
        # 中断后不再刷新，保护 Ctrl+C 后的 print() 输出
        if STOP_EVENT.is_set():
            return

        with self.lock:
            now = time.monotonic()
            if now - self.last_draw < 0.2:
                return
            self.last_draw = now

            pct = self.completed / self.total if self.total else 0
            bar_len = 30
            fill = int(bar_len * pct)
            bar = '#' * fill + ' ' * (bar_len - fill)

            lines = list(self.logs)
            lines.append(
                f"总进度: [{bar}] {self.completed}/{self.total} ({pct:.0%})"
            )

            if self.enable_ansi:
                try:
                    term_width = shutil.get_terminal_size().columns or 80
                except OSError:
                    term_width = 80

                # 上移 N 行并清除至屏尾，然后重绘
                if hasattr(self, '_last_line_count'):
                    sys.stdout.write(
                        f'\033[{self._last_line_count}A\033[J'
                    )
                total_physical = 0
                for line in lines:
                    wrapped = wrap_by_display_width(line, term_width - 2)
                    for wline in wrapped:
                        print(wline, flush=True)
                    total_physical += len(wrapped)
                self._last_line_count = total_physical
            else:
                output = '\n'.join(lines)
                print(output, flush=True)

    def flush(self):
        """强制立即绘制（绕过节流），用于处理完成后的最终刷新。"""
        self.last_draw = 0
        self.draw()
