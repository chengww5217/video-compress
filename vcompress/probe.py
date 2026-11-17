"""ffprobe 探测与「这个文件要不要压」的判定。"""

import json
import logging
import shutil
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import proc
from .util import to_float, to_int

log = logging.getLogger("vcompress")

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
    rotation: int = 0
    fps: float = 0.0
    avg_fps: float = 0.0
    # VFR 片源不能按容器标称帧率压：30fps 段会被悄悄抽成 24.75fps。
    # target_fps/rate 是实际编码（以及 VMAF 抽样）用的统一口径。
    vfr: bool = False
    target_fps: float = 0.0
    target_rate: str = ""
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

    @property
    def display_width(self) -> int:
        """转正后画面的宽。

        width/height 是编码尺寸；带 90/270 度 Display Matrix 的片源，播放器
        看到的是转置后的尺寸。ffmpeg 默认按标记自动旋转后再编码，所以产物
        校验必须比这个值，否则「1088x1920 + 旋转 90°」编成「1920x1088」
        会被误判成分辨率变了。
        """
        return self.height if self.rotation % 180 else self.width

    @property
    def display_height(self) -> int:
        """转正后画面的高，见 display_width。"""
        return self.width if self.rotation % 180 else self.height

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

    @property
    def encode_fps(self) -> float:
        """编码、关键帧间隔和 VMAF 抽样共用的帧率。"""
        return self.target_fps if self.target_fps > 0 else self.fps

    @property
    def encode_rate(self) -> str:
        """ffmpeg `-r` 的参数；空串表示不知道帧率，交给 ffmpeg 的默认行为。"""
        return self.target_rate or _rate_string(self.encode_fps)


def _parse_fps(rate: str) -> float:
    text = str(rate or "").strip()
    if not text:
        return 0.0
    try:
        if "/" in text:
            num, den = text.split("/")
            den = float(den)
            if den == 0:
                return 0.0
            return float(num) / den
        # ffmpeg 的 -r 也可以直接写 30；归一化后的 target_rate 就是这种形式
        return float(text)
    except (ValueError, AttributeError):
        return 0.0


# ---------------------------------------------------------------- 可变帧率

# 帧时长分桶到 10µs：0.033333 和 0.033334 这种量化差异会落进同一桶，
# 23.976fps（41708µs）和 24fps（41667µs）仍分得开。
RATE_BUCKET_US = 10
# 一个帧率至少要有这么多帧才认。时间戳毛刺常常只影响一两帧；而真正的高
# 帧率段哪怕只有 0.1 秒，在 120fps 下也有 12 帧。
MIN_RATE_SAMPLES = 3
# 时间戳坏掉时可能造出 1ms 一帧的「1000fps」。限制在编码器真正有意义的范围。
MIN_RATE_FPS = 1.0
MAX_RATE_FPS = 240.0

# 常见帧率的规范写法。用 packet 时长倒数算出来的是 30.0003 这种带误差的
# 数，换成精确的 30 再交给 ffmpeg，容器里才是干净的帧率。
STANDARD_RATES = (
    (24000 / 1001, "24000/1001"),
    (24.0, "24"),
    (25.0, "25"),
    (30000 / 1001, "30000/1001"),
    (30.0, "30"),
    (48.0, "48"),
    (50.0, "50"),
    (60000 / 1001, "60000/1001"),
    (60.0, "60"),
    (100.0, "100"),
    (120.0, "120"),
    (240.0, "240"),
)

VFR_UNRESOLVED_REASON = (
    "可变帧率（VFR），但探测不出最高帧率；按标称帧率压会丢帧，跳过"
)


def _rate_string(fps: float, prefer: str = "") -> str:
    """把浮点帧率换成 ffmpeg `-r` 想要的精确写法。

    容器给的 `r_frame_rate` 本身就是精确有理数（30000/1001），能用就原样
    保留；从 packet 时长算出来的 30.0003 则按常见帧率归一化，避免产物带着
    一个莫名其妙的帧率。
    """
    if fps <= 0:
        return ""
    preferred = _parse_fps(prefer)
    if preferred > 0 and abs(preferred - fps) <= max(0.01, fps * 1e-4):
        return prefer
    for standard, text in STANDARD_RATES:
        if abs(standard - fps) <= max(0.01, standard * 0.001):
            return text
    return f"{fps:.6f}".rstrip("0").rstrip(".")


def _detect_vfr(nominal: float, average: float) -> bool:
    """标称帧率和平均帧率对不上，基本就是混合帧率/VFR。

    0.5% 的容差是留给 CFR 片源的量化误差（29.97 写成 30、mkv 的毫秒
    时间基）；这个文件是 24.75 对 27.85，差 12.5%，远远超过。
    """
    if nominal <= 0 or average <= 0:
        return False
    return abs(nominal - average) > max(0.05, nominal * 0.005)


def _max_rate_from_durations(durations) -> float:
    """从 packet 时长里找全片出现过的最高帧率。

    取最短时长能保证不丢帧，但单个损坏包可能造出 1ms 的假帧率。所以按时长
    分桶计数，至少要出现 MIN_RATE_SAMPLES 次才认；再按从短到长找第一个
    过门槛的桶，就是全片出现过的最高帧率。
    """
    counts = Counter()
    total = 0
    for raw in durations:
        try:
            duration = float(raw)
        except (TypeError, ValueError):
            continue
        if not 0 < duration < 10:
            continue
        bucket = int(round(duration * 1_000_000 / RATE_BUCKET_US)) * RATE_BUCKET_US
        if bucket <= 0:
            continue
        counts[bucket] += 1
        total += 1
    if not counts:
        return 0.0
    threshold = max(MIN_RATE_SAMPLES, round(total * 0.00001))
    for bucket in sorted(counts):
        rate = 1_000_000 / bucket
        if not MIN_RATE_FPS <= rate <= MAX_RATE_FPS:
            continue
        if counts[bucket] >= threshold:
            return rate
    return 0.0


def _probe_max_rate(ffprobe: str, path: Path) -> float:
    """扫全片 packet 时长，返回出现过的最高帧率；失败返回 0。

    只对 VFR 片源做。通读一遍容器层（7.5GB 实测约一分钟），相对几小时的
    编码可以忽略；抽样几个窗口则可能正好漏掉一段 60fps，压出来就丢帧了。
    """
    cmd = [
        ffprobe, "-v", "error",
        "-select_streams", "V:0",
        "-show_entries", "packet=duration_time",
        "-of", "csv=p=0",
        str(path),
    ]
    try:
        rc, out, err = proc.run_managed(cmd)
    except proc.Cancelled:
        raise
    if rc != 0:
        log.warning("扫描可变帧率失败（ffprobe 退出码 %s）：%s",
                    rc, (err or "").strip()[:200])
        return 0.0
    return _max_rate_from_durations(out.splitlines())


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


def _rotation(vstream: dict) -> int:
    """视频的显示旋转角（0/90/180/270）。

    新容器把旋转写在 Display Matrix side data 里，老一些的 mkv/avi 只有
    stream tag rotate，两处都要看。ffprobe 报的符号表示旋转方向，对判断
    宽高是否互换来说没有区别，统一归一化成非负数。
    """
    for entry in vstream.get("side_data_list") or ():
        if not isinstance(entry, dict) or entry.get("rotation") is None:
            continue
        value = to_int(entry.get("rotation"))
        if value:
            return value % 360
    tags = {str(k).lower(): v for k, v in (vstream.get("tags") or {}).items()}
    value = to_int(tags.get("rotate"))
    return value % 360 if value else 0


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
    info.rotation = _rotation(vstream)
    nominal_rate = vstream.get("r_frame_rate") or ""
    average_rate = vstream.get("avg_frame_rate") or ""
    info.fps = _parse_fps(nominal_rate) or _parse_fps(average_rate)
    info.avg_fps = _parse_fps(average_rate) or info.fps
    info.vfr = _detect_vfr(info.fps, info.avg_fps)
    if info.vfr:
        # 混合帧率必须按全片最高帧率做 CFR：按标称 24.75 压，30fps 段会被
        # 每六帧丢一帧；只有 -r 30 才会把 24.75 段补成重复帧而不是丢 30fps 段。
        target = _probe_max_rate(tools.ffprobe, path)
        info.target_rate = _rate_string(target, prefer=nominal_rate)
        # 以规范写法的有理数为准：packet 时长倒数算出来是 30.003，拿它做
        # 帧对齐会在片尾漂出十几帧，切点又压回帧边界上。实际编码用的是
        # -r 30（精确 30fps），抽样也必须用同一个数。
        info.target_fps = _parse_fps(info.target_rate)
    else:
        info.target_rate = _rate_string(info.fps, prefer=nominal_rate)
        info.target_fps = _parse_fps(info.target_rate) or info.fps
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

    # 探测不出 VFR 的最高帧率时不能硬压：ffmpeg 会按标称帧率做 CFR，
    # 高帧率段直接被抽帧，而 VMAF 未必看得出来（它比的也是抽过帧的片段）。
    if info.vfr and info.target_fps <= 0:
        return True, VFR_UNRESOLVED_REASON

    min_size = int(cfg["skip"].get("min_size_mb", 0)) * 1024 * 1024
    if info.size and info.size < min_size:
        return True, f"文件太小（{info.size // 1024 // 1024} MB）"

    bpp_cfg = cfg["skip"]["bpp"]
    if info.codec_family and info.codec_family in bpp_cfg:
        # 混合帧率片源按平均帧率算 BPP 才接近真实码率；用标称 24.75 会把
        # 30fps 段的每像素码率算高，可能把一个已经压到位的文件放进编码队列。
        fps = info.avg_fps if info.avg_fps > 0 else info.fps
        if fps <= 0:
            fps = 25.0
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
