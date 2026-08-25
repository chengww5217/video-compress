"""这个 ffmpeg 构建能干什么：编码器/滤镜清单与像素格式决策。

两件事放一起的理由：都是「运行时问 ffmpeg」而不是「怎么编码」。硬编码一张
能力表迟早和实际构建脱节（各发行版的 svt-av1 版本差异很大），所以清单在启动
时问一次、像素格式按 (ffmpeg, codec) 缓存，之后全程复用。
"""

import re
import threading
from dataclasses import dataclass

from . import proc
from .util import to_int

_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")
_DEPTH_RE = re.compile(r"(\d{1,2})(?:[lb]e)?$")


# ---------------------------------------------------------------- 构建能力探测

@dataclass(frozen=True)
class Caps:
    """一次性问出来的 ffmpeg 构建能力，避免每个文件都重复问。"""

    encoders: frozenset = frozenset()
    filters: frozenset = frozenset()

    @classmethod
    def probe(cls, ffmpeg: str) -> "Caps":
        return cls(encoders=frozenset(available_encoders(ffmpeg)),
                   filters=frozenset(available_filters(ffmpeg)))

    @property
    def known(self) -> bool:
        return bool(self.encoders)

    @property
    def lossless_codec(self) -> str:
        """抽样片段用的无损编码器。

        libx264 -crf 0 又快又小，是首选；构建里没有它时退回 ffv1——ffv1 是
        ffmpeg 自带的（不依赖外部库），任何构建都有，代价是文件大不少。
        """
        if not self.encoders or "libx264" in self.encoders:
            return "libx264"
        return "ffv1"


def _parse_encoder_names(text: str) -> set:
    """解析 ffmpeg -encoders 输出，取出编码器名字（跳过开头的图例行）。"""
    names = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS" \
                and _NAME_RE.fullmatch(parts[1]):
            names.add(parts[1])
    return names


def _parse_filter_names(text: str) -> set:
    names = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and len(parts[0]) <= 3 and "->" in parts[2] \
                and _NAME_RE.fullmatch(parts[1]):
            names.add(parts[1])
    return names


def available_encoders(ffmpeg: str) -> set:
    try:
        rc, out, _err = proc.run_managed([ffmpeg, "-hide_banner", "-encoders"])
    except OSError:
        return set()
    return _parse_encoder_names(out) if rc == 0 else set()


def available_filters(ffmpeg: str) -> set:
    try:
        rc, out, _err = proc.run_managed([ffmpeg, "-hide_banner", "-filters"])
    except OSError:
        return set()
    return _parse_filter_names(out) if rc == 0 else set()


# ---------------------------------------------------------------------- 像素格式

# 查不到时的兜底：只列最关键的约束，libsvtav1 只吃 4:2:0
_FALLBACK_PIX_FMTS = {
    "libsvtav1": {"yuv420p", "yuv420p10le"},
    "libaom-av1": {"yuv420p", "yuv420p10le", "yuv420p12le", "yuv422p",
                   "yuv422p10le", "yuv444p", "yuv444p10le"},
}

_pix_fmt_cache = {}
_pix_fmt_lock = threading.Lock()

_SUPPORTED_RE = re.compile(r"Supported pixel formats:\s*(.+)")


def encoder_pix_fmts(ffmpeg: str, codec: str) -> set:
    """问 ffmpeg 这个编码器支持哪些像素格式，问不出来就用兜底表。

    硬编码一张表迟早和实际构建脱节（发行版的 svt-av1 版本差异很大），所以
    优先运行时查询，结果按 (ffmpeg, codec) 缓存。
    """
    key = (ffmpeg, codec)
    with _pix_fmt_lock:
        if key in _pix_fmt_cache:
            return _pix_fmt_cache[key]
    fmts = set()
    try:
        rc, out, err = proc.run_managed(
            [ffmpeg, "-hide_banner", "-h", f"encoder={codec}"])
        if rc == 0:
            m = _SUPPORTED_RE.search(out) or _SUPPORTED_RE.search(err)
            if m:
                fmts = {t for t in m.group(1).split() if _NAME_RE.fullmatch(t)}
    except OSError:
        fmts = set()
    if not fmts:
        fmts = set(_FALLBACK_PIX_FMTS.get(codec, ()))
    with _pix_fmt_lock:
        _pix_fmt_cache[key] = fmts
    return fmts


def pix_fmt_depth(pix_fmt: str) -> int:
    m = _DEPTH_RE.search(pix_fmt or "")
    if not m:
        return 8
    depth = int(m.group(1))
    return depth if 8 <= depth <= 16 else 8


def choose_pix_fmt(src_pix_fmt: str, supported) -> str | None:
    """源像素格式编码器不支持时，挑一个最接近的替代格式。

    返回 None 表示不用管（本来就支持，或者信息不足别乱改）。优先保住位深，
    因为位深降级看得出来，色度抽样（4:2:2 -> 4:2:0）在消费级片源上几乎看
    不出来。
    """
    if not supported or not src_pix_fmt or src_pix_fmt in supported:
        return None
    depth = pix_fmt_depth(src_pix_fmt)
    candidates = []
    for d in (depth, 12, 10, 8):
        candidates.append("yuv420p" if d <= 8 else f"yuv420p{d}le")
    for d in (depth, 12, 10, 8):
        candidates.append("yuv422p" if d <= 8 else f"yuv422p{d}le")
        candidates.append("yuv444p" if d <= 8 else f"yuv444p{d}le")
    seen = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        if cand in supported:
            return cand
    return None


def bumped_pix_fmt(pix_fmt: str, supported, min_depth: int):
    """位深低于 min_depth 时提到 min_depth，色度抽样保持不变。

    8-bit 源用 10-bit 编码是 AV1/HEVC 上公认的免费收益：编码器内部精度更高，
    暗部渐变不容易出现 banding，同画质下码率还略低一点。代价只是编解码稍慢，
    而 AV1 Main / HEVC Main10 都是必备档位，兼容性没有实际影响。

    只动位深不动色度：4:2:2/4:4:4 的源（ProRes 之类）在支持它们的编码器上要
    原样保留，把它们压成 4:2:0 才是真丢信息。认不出格式族时返回 None 不动。
    """
    if min_depth <= 8 or not pix_fmt or not supported:
        return None
    if pix_fmt_depth(pix_fmt) >= min_depth:
        return None
    m = re.match(r"^(yuv[0-9]{3}p|gbrp)(?:\d{1,2}(?:[lb]e)?)?$", pix_fmt)
    if not m:
        return None
    family = m.group(1)
    for depth in (min_depth, 12, 16):
        if depth < min_depth:
            continue
        cand = f"{family}{depth}le"
        if cand in supported:
            return cand
    return None


def with_pix_fmt(encoder_cfg: dict, src_pix_fmt: str, supported,
                 log=None) -> dict:
    """给编码器配置补上 -pix_fmt，必要时告警说明降级到了什么。

    不做这件事的后果：libsvtav1 遇到 4:2:2 源（ProRes、部分 mov/avi）会直接
    报 "Specified pixel format yuv422p is invalid or not supported" 整个文件失败。
    """
    cfg = dict(encoder_cfg)
    if cfg.get("pix_fmt"):
        return cfg  # 用户显式指定了，不猜
    chosen = choose_pix_fmt(src_pix_fmt, supported)
    if chosen:
        cfg["pix_fmt"] = chosen
        if log:
            log.warning(f"{cfg['codec']} does not accept {src_pix_fmt}; "
                        f"converting to {chosen}")
    elif supported and src_pix_fmt and src_pix_fmt not in supported:
        if log:
            log.warning(f"{cfg['codec']} does not accept {src_pix_fmt} and no "
                        f"close substitute is available; the encode may fail")
    current = cfg.get("pix_fmt") or src_pix_fmt
    bumped = bumped_pix_fmt(current, supported,
                            to_int(cfg.get("min_bit_depth"), 10))
    if bumped:
        cfg["pix_fmt"] = bumped
        if log:
            log.info(f"encoding {current} source as {bumped} "
                     f"(higher internal precision, less banding)")
    return cfg
