"""无依赖的小工具：类型宽松转换与时长格式化。

probe/verify/cli 三处原本各写了一份 int/float 兜底转换，语义还不完全一样
（一边 int(float(v))，一边 int(v)，对 "3.7" 结果不同）。统一到这里。
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


def fmt_size(num_bytes) -> str:
    """字节 -> 人类可读；只用于日志。"""
    n = float(to_float(num_bytes, 0.0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"
