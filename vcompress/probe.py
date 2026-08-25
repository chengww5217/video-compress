"""ffprobe 探测与「这个文件要不要压」的判定。"""

import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import proc
from .util import to_float, to_int

CODEC_FAMILY = {
    "h264": "h264",
    "avc1": "h264",
    "hevc": "hevc",
    "hev1": "hevc",
    "hvc1": "hevc",
    "h265": "hevc",
    "av01": "av1",
    "av1": "av1",
    "vp9": "vp9",
    "vp09": "vp9",
    "mpeg2video": "mpeg2video",
    "mpeg4": "mpeg4",
    "flv1": "flv1",
    "wmv3": "wmv3",
}

# ffprobe 报出来的 side data 名字 -> 我们关心的短名。只认这几种：它们丢了
# 画面数值可能一模一样（VMAF 照样 99 分），但播放器少了峰值亮度/母版信息，
# HDR 会被错误地当成 SDR 显示。其余 side data（各种 SEI）不影响观感，忽略。
HDR_SIDE_DATA = {
    "mastering display metadata": "mastering-display",
    "content light level metadata": "content-light-level",
    "dolby vision rpu data": "dolby-vision",
    "dolby vision metadata": "dolby-vision",
    "dolby vision configuration record": "dolby-vision",
    "hdr dynamic metadata smpte2094-40 (hdr10+)": "hdr10plus",
    "hdr dynamic metadata smpte2094-40": "hdr10plus",
}

# ffprobe 对「没标」的写法不止一种，都当作源没提供这项信息
UNSPECIFIED_COLOR = {"", "unknown", "unspecified", "reserved", "n/a"}

# field_order 的这几个值表示真交错（顶场优先/底场优先及其两种存储顺序）。
# progressive、unknown 以及压根没这个字段的都按渐进处理——绝大多数文件不标它。
INTERLACED_FIELD_ORDERS = {"tt", "bb", "tb", "bt"}

if sys.platform == "win32":
    _FALLBACK_DIRS = (r"C:\Program Files\ffmpeg\bin", r"C:\Program Files\ffmpeg")
else:
    _FALLBACK_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")


def _find_tool(config_value: str, name: str) -> str:
    def resolve(v):
        p = Path(v).expanduser()
        if p.is_dir():
            for cand in (p / f"{name}.exe", p / name):
                if cand.exists():
                    return str(cand)
            return None
        if p.exists():
            return str(p)
        return None

    if config_value:
        r = resolve(config_value)
        if r:
            return r
        raise FileNotFoundError(
            f"config.toml [tools] 指定了 {config_value!r}，但那里找不到 {name}"
        )
    w = shutil.which(name)
    if w:
        return w
    for d in _FALLBACK_DIRS:
        r = resolve(d)
        if r:
            return r
    raise FileNotFoundError(
        f"找不到 {name}：请安装 ffmpeg，或在 config.toml [tools] 里指定路径"
    )


class Tools:
    def __init__(self, cfg: dict):
        self.ffmpeg = _find_tool(cfg.get("ffmpeg", ""), "ffmpeg")
        self.ffprobe = _find_tool(cfg.get("ffprobe", ""), "ffprobe")


@dataclass
class VideoInfo:
    path: Path
    container: str
    video_codec: str = ""
    codec_family: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    bitrate: int = 0
    container_bitrate: int = 0
    duration: float = 0.0
    size: int = 0
    pix_fmt: str = ""
    field_order: str = ""
    comment: str = ""
    color_primaries: str = ""
    color_transfer: str = ""
    color_space: str = ""
    color_range: str = ""
    hdr_metadata: list = field(default_factory=list)
    audio_codecs: list = field(default_factory=list)
    audio_bitrates: list = field(default_factory=list)
    audio_channels: list = field(default_factory=list)
    subtitle_codecs: list = field(default_factory=list)

    def video_bitrate(self) -> int:
        """视频轨码率；容器没给的话用总码率扣掉音轨估算。"""
        if self.bitrate > 0:
            return self.bitrate
        if self.container_bitrate > 0:
            audio = sum(b for b in self.audio_bitrates if b)
            rest = self.container_bitrate - audio
            return rest if rest > 0 else self.container_bitrate
        return 0

    @property
    def interlaced(self) -> bool:
        return self.field_order in INTERLACED_FIELD_ORDERS


def _parse_fps(rate: str) -> float:
    try:
        num, den = rate.split("/")
        den = float(den)
        if den == 0:
            return 0.0
        return float(num) / den
    except (ValueError, AttributeError):
        return 0.0


def _stream_bitrate(stream: dict) -> int:
    """matroska 通常不给 bit_rate，退回它写的 BPS 统计标签。"""
    tags = {k.lower(): v for k, v in (stream.get("tags") or {}).items()}
    bps = next((v for k, v in tags.items() if k == "bps" or k.startswith("bps-")),
               None)
    return to_int(stream.get("bit_rate")) or to_int(bps)


def _color_tag(value) -> str:
    """规范化色彩属性值；没标的一律返回空串。"""
    text = str(value or "").strip().lower()
    return "" if text in UNSPECIFIED_COLOR else text


def _hdr_markers(*side_data_lists) -> list:
    """从若干 side_data_list 里挑出我们关心的 HDR 标记，去重后排序。

    HDR10 的母版信息在有些容器里是流级 side data，在有些片源里只以帧内 SEI
    的形式存在（x265 写的就是后者），所以两边都要看。
    """
    found = set()
    for entries in side_data_lists:
        for entry in entries or ():
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("side_data_type") or "").strip().lower()
            marker = HDR_SIDE_DATA.get(name)
            if marker:
                found.add(marker)
    return sorted(found)


def probe(tools: Tools, path: Path) -> VideoInfo:
    cmd = [
        tools.ffprobe,
        "-v", "error",
        "-show_format",
        "-show_streams",
        # 只读第一帧：HDR 母版信息可能只存在于帧内 SEI，不看帧就看不到它
        "-show_frames",
        "-read_intervals", "%+#1",
        "-of", "json",
        str(path),
    ]
    # 走 run_managed：Ctrl+C 时这个 ffprobe 也要被杀掉，别留孤儿进程
    rc, out, err = proc.run_managed(cmd)
    if rc != 0:
        raise RuntimeError(f"ffprobe 读不了 {path.name}：{err.strip()[:300]}")
    try:
        data = json.loads(out)
    except ValueError as e:
        raise RuntimeError(f"ffprobe 对 {path.name} 返回的 JSON 解析不了：{e}") from e
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    vstream = next((s for s in streams if s.get("codec_type") == "video"), {})
    astreams = [s for s in streams if s.get("codec_type") == "audio"]
    sstreams = [s for s in streams if s.get("codec_type") == "subtitle"]

    info = VideoInfo(path=path, container=path.suffix.lower())
    info.video_codec = vstream.get("codec_name", "")
    info.codec_family = CODEC_FAMILY.get(info.video_codec, "")
    info.width = to_int(vstream.get("width"))
    info.height = to_int(vstream.get("height"))
    info.fps = _parse_fps(vstream.get("r_frame_rate")) or \
        _parse_fps(vstream.get("avg_frame_rate"))
    info.pix_fmt = vstream.get("pix_fmt", "")
    info.field_order = str(vstream.get("field_order") or "").strip().lower()
    info.color_primaries = _color_tag(vstream.get("color_primaries"))
    info.color_transfer = _color_tag(vstream.get("color_transfer"))
    info.color_space = _color_tag(vstream.get("color_space"))
    info.color_range = _color_tag(vstream.get("color_range"))
    vframe = next((f for f in data.get("frames") or []
                   if f.get("media_type") == "video"), {})
    info.hdr_metadata = _hdr_markers(vstream.get("side_data_list"),
                                     vframe.get("side_data_list"))
    info.bitrate = _stream_bitrate(vstream)
    info.container_bitrate = to_int(fmt.get("bit_rate"))
    info.duration = to_float(fmt.get("duration")) or \
        to_float(vstream.get("duration"))
    info.size = to_int(fmt.get("size"), default=0) or _file_size(path)
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    info.comment = str(tags.get("comment") or tags.get("description") or "")
    info.audio_codecs = [s.get("codec_name", "") for s in astreams]
    info.audio_bitrates = [_stream_bitrate(s) for s in astreams]
    info.audio_channels = [to_int(s.get("channels"), 2) for s in astreams]
    info.subtitle_codecs = [s.get("codec_name", "") for s in sstreams]
    return info


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def should_skip(info: VideoInfo, cfg: dict):
    marker = str(cfg["skip"].get("tag_marker", "vcompress"))
    if marker and marker.lower() in (info.comment or "").lower():
        return True, f"元数据里已有 {marker} 标记，之前压过了"

    # 交错源：像素能原样压过去，但容器的交错标记压根传不过去（AV1 没有交错
    # 信号），播放器从此不再反交错，梳齿就留在画面上了。这是 VMAF 测不出来的
    # 视觉有损，和色彩元数据丢失同一类，所以不能压——更不能压完删掉原文件。
    # 反交错属于「动画面」，不在本工具范围内，唯一正确的处置就是不碰。
    if cfg["skip"].get("skip_interlaced", True) and info.interlaced:
        return True, (
            f"交错源（field_order={info.field_order}）：压过去会丢掉交错标记，"
            f"播放器不再反交错"
        )

    min_size = int(cfg["skip"].get("min_size_mb", 0)) * 1024 * 1024
    if info.size and info.size < min_size:
        return True, f"文件太小（{info.size // 1024 // 1024} MB）"

    bpp_cfg = cfg["skip"]["bpp"]
    if info.codec_family and info.codec_family in bpp_cfg:
        fps = info.fps if info.fps > 0 else 25.0
        pixels = info.width * info.height
        vbr = info.video_bitrate()
        if vbr > 0 and pixels > 0:
            bpp = vbr / (pixels * fps)
            thr = float(bpp_cfg[info.codec_family])
            if bpp < thr:
                return True, (
                    f"{info.codec_family} 每像素码率 {bpp:.4f} bits，"
                    f"已经低于阈值 {thr}，视为压到位了"
                )

    ratio_cfg = cfg["skip"]["expected_ratio"]
    min_saving = float(cfg["skip"].get("min_saving_ratio", 0.15))
    if info.codec_family and info.codec_family in ratio_cfg:
        predicted = float(ratio_cfg[info.codec_family])
        saving = 1.0 - predicted
        if saving < min_saving:
            return True, (
                f"{info.codec_family} 预估只能省 {saving:.0%}，"
                f"低于要求的 {min_saving:.0%}"
            )
    return False, ""
