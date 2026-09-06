"""无依赖的小工具：类型宽松转换、时长与体积格式化。

probe/verify/cli 三处原本各写了一份 int/float 兜底转换，语义还不完全一样
（一边 int(float(v))，一边 int(v)，对 "3.7" 结果不同）。统一到这里。
体积格式化同理：几处各拼一份 f"{n} 字节" 的话，同一个数在不同消息里长得不一样。
"""


def to_int(value, default: int = 0) -> int:
    """尽量转成 int；ffprobe 会把整数写成 "6000000" 也会写成 "6000000.0"。"""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fmt_hms(seconds) -> str:
    """秒 -> H:MM:SS，负数按 0 处理。"""
    s = max(0, int(to_float(seconds, 0.0)))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


# 1024 进制。日志里给人看的单位，不用 KiB/MiB 那套写法——这个工具的读者是
# 拿它压电影的人，看到 "1.44 GB" 就够了。
SIZE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")

# 最多两位小数。大单位少了两位就分不出 1.44 GB 和 1.49 GB——恰好是"产物比源
# 文件大了多少"这种判断要看的精度；但多余的零不留，2.00 GB 写成 2 GB。
SIZE_DECIMALS = 2


def _trim_zeros(text: str) -> str:
    """去掉小数里多余的零：2.00 -> 2，2.10 -> 2.1，1.44 原样。

    只在有小数点时动手，否则 "100" 会被 rstrip("0") 啃成 "1"。
    """
    return text.rstrip("0").rstrip(".") if "." in text else text


def fmt_size(num_bytes) -> str:
    """字节 -> 人类可读，如 512 B / 48 KB / 1.44 GB / 2 TB。

    日志和错误信息里的体积一律走这里，别自己拼 f"{n} 字节"：几处各写一份的
    时候，同一个数在不同消息里会长得不一样，读日志的人得自己换算。
    """
    n = float(to_float(num_bytes, 0.0))
    for unit in SIZE_UNITS[:-1]:
        if abs(n) < 1024:
            return f"{_trim_zeros(f'{n:.{SIZE_DECIMALS}f}')} {unit}"
        n /= 1024
    return f"{_trim_zeros(f'{n:.{SIZE_DECIMALS}f}')} {SIZE_UNITS[-1]}"


def fmt_size_change(before, after) -> str:
    """体积变化 -> "1.44 GB -> 1.49 GB（+3.5%）"，正号表示涨了。

    两边格式化后一模一样、但字节数其实不同时退回精确字节：不然会打出
    "1.44 GB -> 1.44 GB" 这种看起来像 bug 的话，而这个函数最常用在
    "这个文件和上次不是同一个了" 的场合，那里差几百字节也算不同。
    """
    b = to_int(before)
    a = to_int(after)
    left, right = fmt_size(b), fmt_size(a)
    if left == right and a != b:
        # 差异小到被四舍五入吃掉了，百分比这时也只是 "-0.0%" 这种噪音
        return f"{b} 字节 -> {a} 字节"
    if b <= 0:
        return f"{left} -> {right}"  # 除不了就别硬算
    return f"{left} -> {right}（{a / b - 1:+.1%}）"
