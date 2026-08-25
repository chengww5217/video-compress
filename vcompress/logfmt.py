"""日志呈现：控制台上色、重点高亮，文件日志的会话分隔。

日志文本一律用中文——这个工具是人看着跑的，出问题时要一眼读懂。

控制台和文件两路刻意分工：

* **控制台**：INFO 起，带颜色。级别用颜色区分，行首的 `[编码]`/`[完成]` 这类
  状态标签单独上色，消息里用 `em()` 包住的重点值（文件名、CRF、VMAF、体积）
  加粗高亮。不是终端（重定向到文件、CI）或设了 `NO_COLOR` 时自动退化成纯文本。
* **文件**：DEBUG 起，不上色（ANSI 转义写进文件只会碍事），带月日时间。文件是
  追加写的，所以每次启动先空几行、打一条 `==== 0825 21:30:01 启动程序 ====`，
  翻历史时好找到某一次运行从哪儿开始。
"""

import logging
import os
import re
import sys
import time
from pathlib import Path

# 高亮区间的标记。用不可打印字符是为了不和文件名、路径里的字符撞车；
# 两个 formatter 都必须把它处理掉，文件日志里不会留下这两个字节。
EM_START = "\x02"
EM_END = "\x03"

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
BLUE = "\x1b[34m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"

# 级别 -> (中文标签, 整行颜色, 高亮颜色)
LEVEL_STYLES = {
    logging.DEBUG: ("调试", DIM, ""),
    logging.INFO: ("信息", "", BOLD + CYAN),
    logging.WARNING: ("警告", YELLOW, BOLD + YELLOW),
    logging.ERROR: ("错误", RED, BOLD + RED),
    logging.CRITICAL: ("致命", BOLD + RED, BOLD + RED),
}

# 行首状态标签的颜色。标签本身是消息的一部分（文件日志里照样能 grep），
# 这里只负责让它在控制台上跳出来。
TAG_STYLES = {
    "[完成]": BOLD + GREEN,
    "[汇总]": BOLD + CYAN,
    "[编码]": BOLD + BLUE,
    "[搜索]": BLUE,
    "[预估]": BLUE,
    "[校验]": MAGENTA,
    "[跳过]": DIM,
    "[缓存]": DIM,
    "[重查]": CYAN,
    "[预览]": CYAN,
    "[失败]": BOLD + RED,
    "[中止]": BOLD + YELLOW,
}

_TAG_RE = re.compile(r"^(\[[^\]\s]{1,6}\])")


def em(value) -> str:
    """标记一段"重点"，控制台上会高亮，文件日志里原样保留文字。"""
    return f"{EM_START}{value}{EM_END}"


def strip_marks(text: str) -> str:
    return text.replace(EM_START, "").replace(EM_END, "")


def supports_color(stream) -> bool:
    """终端才上色；NO_COLOR / FORCE_COLOR 按事实标准处理。"""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        if not stream.isatty():
            return False
    except Exception:
        return False
    if os.name == "nt":
        return _enable_windows_ansi()
    return True


def _enable_windows_ansi() -> bool:
    """Windows 10 起的控制台要显式打开 VT 处理才认 ANSI 转义。"""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


class ConsoleFormatter(logging.Formatter):
    """时间 + 中文级别 + 消息；带色时高亮状态标签和重点值。"""

    def __init__(self, color: bool):
        super().__init__("%(message)s")
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime("%H:%M:%S", self.converter(record.created))
        label, line_style, em_style = LEVEL_STYLES.get(
            record.levelno, ("信息", "", BOLD))
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        if not self.color:
            return f"{stamp} {label} {strip_marks(message)}"

        tag = ""
        m = _TAG_RE.match(strip_marks(message))
        if m and m.group(1) in TAG_STYLES:
            tag = m.group(1)
            message = message[len(tag):].lstrip()
            # 警告/错误行里标签跟着整行的颜色走，不然一行里两种色调打架
            style = em_style if record.levelno >= logging.WARNING \
                else TAG_STYLES[tag]
            tag = f"{style}{tag}{RESET} "

        if em_style:
            body = message.replace(EM_START, em_style) \
                          .replace(EM_END, RESET + line_style)
        else:
            body = strip_marks(message)
        if line_style:
            body = f"{line_style}{body}{RESET}"
            label = f"{line_style}{label}{RESET}"
        return f"{DIM}{stamp}{RESET} {label} {tag}{body}"


class FileFormatter(logging.Formatter):
    """文件里不上色，但要有日期：这份日志是跨天追加的。"""

    def __init__(self):
        super().__init__("%(message)s")

    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime("%m%d %H:%M:%S", self.converter(record.created))
        message = strip_marks(record.getMessage())
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        return f"{stamp} {record.levelname:<7} {message}"


def session_banner(argv=None) -> str:
    stamp = time.strftime("%m%d %H:%M:%S")
    line = f"==== {stamp} 启动程序 ===="
    parts = [line]
    if argv:
        parts.append(f"命令行: {' '.join(argv)}")
    return "\n".join(parts)


def write_session_banner(log_file: Path, argv=None) -> None:
    """在追加写的日志文件里插一条会话分隔，前面空几行好找。"""
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write("\n\n\n" + session_banner(argv) + "\n")
    except OSError:
        pass  # 日志分隔线写不进去不值得让整次运行失败
