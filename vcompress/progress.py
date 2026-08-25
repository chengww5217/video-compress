"""单行进度显示。

只在真正串行执行时启用：并发时几个文件的输出会互相打断 \\r 行，反而更难读。
"""

import sys

from . import logfmt
from .util import fmt_hms

NAME_WIDTH = 36


class ProgressPrinter:
    """把 run_encode 的回调渲染成一行可刷新的进度。

    close() 必须在写任何日志之前调用，否则日志会直接粘在 \\r 行尾。
    """

    def __init__(self, name: str, enabled: bool = True, stream=None):
        self.name = name[:NAME_WIDTH]
        self.enabled = enabled
        self.stream = stream if stream is not None else sys.stdout
        self.color = enabled and logfmt.supports_color(self.stream)
        self._open = False

    def _pct(self, pct: float) -> str:
        text = f"{pct:5.1f}%"
        return f"{logfmt.BOLD}{logfmt.CYAN}{text}{logfmt.RESET}" \
            if self.color else text

    def __call__(self, pct, info_val=0.0, speed=0.0):
        """run_encode 的 progress_cb：pct=None 表示总时长未知，info_val 是已处理秒数。"""
        if not self.enabled:
            return
        if pct is None:
            text = f"\r  已处理 {fmt_hms(info_val)}   {self.name:<{NAME_WIDTH}}"
        else:
            spd = f"x{speed:.1f}" if speed > 0 else "        "
            eta = f"剩余 {fmt_hms(info_val):>7}" if info_val else "剩余      --"
            text = (f"\r  {self._pct(pct)}  {eta}  {spd:>7}  "
                    f"{self.name:<{NAME_WIDTH}}")
        self.stream.write(text)
        self.stream.flush()
        self._open = True

    def close(self) -> None:
        if self._open:
            self.stream.write("\n")
            self.stream.flush()
            self._open = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
