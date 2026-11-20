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
    vfr_max_fps: float = 0.0
    # 目标帧率低于全片最高帧率时，高于目标的那部分帧会被丢弃；这是源帧
    # 总数里的比例，只允许是「少量」（见 [encode] vfr_max_drop_ratio）。
    vfr_drop_ratio: float = 0.0
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
    # DTS-HD MA / DTS:X 的 codec_name 也是 dts，只有 profile 能区分
    audio_profiles: list = field(default_factory=list)
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
# 候选帧率占全片帧数的下限。只出现几帧的 60fps 很可能是时间戳毛刺，不能
# 因此把整片从 24fps 抬到 60fps——那会把编码时间翻几倍。低于这个占比的
# 高帧段直接按「可丢弃的少量高帧」处理。
MIN_RATE_SHARE = 0.001
# 时间戳坏掉时可能造出 1ms 一帧的「1000fps」。限制在编码器真正有意义的范围。
MIN_RATE_FPS = 1.0
MAX_RATE_FPS = 240.0
# 同一个真实帧率会被容器的粗时间基量化到相邻两个时长桶：1/1000 时间基下
# 30fps 会写成 33/34ms，两个桶单独看会算出 30.303/29.412。间隔不超过
# 1.2ms 的相邻桶视为同一帧率，按加权平均时长参与分位；24/25fps 这种真正
# 不同的帧率相差 1.67ms，不会被误合并。
RATE_CLUSTER_GAP_US = 1200
# 簇的加权平均时长和标准帧率的周期只差一个量化误差时，换成标准帧率；
# 1.2% 足够覆盖 1/1000 时间基下 30/60fps 的 1ms 取整，又不会把 48/50
# 这种相差 4% 的真实帧率并成一个。
STANDARD_RATE_TOLERANCE = 0.012

# 常见帧率的规范写法。用 packet 时长倒数算出来的是 30.0003 这种带误差的
# 数，换成精确的 30 再交给 ffmpeg，容器里才是干净的帧率。
STANDARD_RATES = (
    (24000 / 1001, "24000/1001"),
    (24.0, "24"),
    (24.75, "99/4"),
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

# BPP 预筛结论的标记。pipeline 要区分「BPP 低」和别的跳过原因，
# 前者可以先用低成本预检再决定，后者（标记/交错/太小/VFR）不能。
BPP_REASON_MARKER = "每像素码率"


def is_bpp_reason(reason: str) -> bool:
    """这条跳过原因是不是 BPP 预筛给出的。"""
    return BPP_REASON_MARKER in (reason or "")


VFR_UNRESOLVED_REASON = (
    "可变帧率（VFR），但探测不出可用的目标帧率；按标称帧率压会丢帧，跳过"
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
    # 标称帧率本身就是精确有理数时原样保留；这里只收几乎相同的值，避免把
    # 已经选好的 29.97 又按标称 30 改回去。
    if preferred > 0 and abs(preferred - fps) <= max(0.01, fps * 1e-4):
        return prefer
    best = None
    for standard, text in STANDARD_RATES:
        diff = abs(standard - fps)
        # 0.2% 的容差要能盖住粗时间基的量化误差：1/1000 时间基下 24.75fps
        # 的时长桶均值可能偏 0.1%。两个标准帧率之间至少差 0.1%（29.97/30），
        # 取最接近的即可，不会把 30 写成 29.97。
        if diff > max(0.01, standard * 2e-3):
            continue
        # 取最接近的，而不是先遇到的：59.988 同时落在 59.94 和 60 的
        # 0.2% 容差里，只有按距离选才不会把 60fps 写成 59.94。
        if best is None or diff < best[0]:
            best = (diff, text)
    if best is not None:
        return best[1]
    return f"{fps:.6f}".rstrip("0").rstrip(".")


def _detect_vfr(nominal: float, average: float) -> bool:
    """标称帧率和平均帧率对不上，基本就是混合帧率/VFR。

    0.5% 的容差是留给 CFR 片源的量化误差（29.97 写成 30、mkv 的毫秒
    时间基）；这个文件是 24.75 对 27.85，差 12.5%，远远超过。
    """
    if nominal <= 0 or average <= 0:
        return False
    return abs(nominal - average) > max(0.05, nominal * 0.005)


def _rate_clusters(counts) -> list:
    """把相邻的时长桶并成帧率簇，返回 [(代表时长µs, 帧数), ...]。

    只合并间隔不超过 RATE_CLUSTER_GAP_US 的相邻桶。容差取所有小间隔里的
    最大值：同一个帧率被 1/1000 时间基量化到 33/34ms 时，间隔是 1ms；
    24fps 和 25fps 的 41.667/40ms 相差 1.667ms，仍分属两簇。

    簇的加权平均时长落在某个标准帧率的 1.2% 以内时，换成该标准的精确
    周期（33/34ms -> 30fps，16/17ms -> 60fps）。落不到标准上且簇里不止
    一个桶时拆回逐桶：48/50fps 的 20/20.833ms 只差 0.833ms，但它们是
    两个真实帧率（相差 4%），并成一个 49fps 反而会丢帧；拆开后分位策略
    会选较高的 50fps，一帧不丢。
    """
    buckets = sorted(counts)
    small_gaps = [b - a for a, b in zip(buckets, buckets[1:])
                  if 0 < b - a <= RATE_CLUSTER_GAP_US]
    tolerance = max(small_gaps) if small_gaps else 0
    groups = []
    current = []
    for bucket in buckets:
        if current and tolerance and bucket - current[-1] <= tolerance:
            current.append(bucket)
        else:
            if current:
                groups.append(current)
            current = [bucket]
    if current:
        groups.append(current)

    clusters = []
    for group in groups:
        count = sum(counts[b] for b in group)
        mean_us = sum(b * counts[b] for b in group) / count
        if len(group) == 1:
            # 只有一个桶时没有“同一帧率被量化成两个桶”的证据，不往标准
            # 帧率上硬掰：33ms 可能就是真实的 30.303fps，压到 30 会在
            # vfr_max_drop_ratio=0 时也丢帧。小幅误差交给 _rate_string。
            clusters.append((mean_us, count))
            continue
        rate = 1_000_000 / mean_us
        standard = min(STANDARD_RATES, key=lambda item: abs(item[0] - rate))
        if abs(standard[0] - rate) <= standard[0] * STANDARD_RATE_TOLERANCE:
            clusters.append((1_000_000 / standard[0], count))
        else:
            # 均值落不到标准帧率上，说明这簇里可能混着两个真实帧率
            # （48/50fps）。拆回逐桶，分位策略会选较高的那个，一帧不丢。
            clusters.extend((b, counts[b]) for b in group)
    return clusters


def _rate_plan_from_durations(durations, max_drop_ratio: float = 0.05) -> tuple:
    """从 packet 时长里选编码目标帧率。

    返回 `(目标帧率, 全片最高可靠帧率, 被丢弃帧的比例)`。

    目标不是无脑取最高：允许丢掉少量高帧片段，换更低的编码帧率。按帧数
    从高到低累计，选「高于它的帧占比不超过 max_drop_ratio」的最低帧率。
    默认 5%：24fps 为主、只有 2% 60fps 的片源按 24fps 压，丢掉那 2%；
    30fps 占 63.7% 的片源仍按 30fps 压，一帧不丢。

    单个损坏包可能造出 1ms 的假帧率，所以候选帧率至少要占全片
    MIN_RATE_SHARE（0.1%）且不少于 MIN_RATE_SAMPLES 帧；时间戳坏掉的
    超范围时长直接忽略。低于门槛的高帧帧仍会算进丢弃比例，不会被偷偷
    当成「没发生」。

    容器的粗时间基会把同一个帧率拆到相邻两个桶（mkv 的 30fps 是
    33/34ms）。先按 `_rate_clusters` 并簇再分位，否则会选出一个
    30.303 这种被量化误差抬高的非标准目标。
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
        rate = 1_000_000 / bucket
        if not MIN_RATE_FPS <= rate <= MAX_RATE_FPS:
            continue
        counts[bucket] += 1
        total += 1
    if not counts:
        return 0.0, 0.0, 0.0

    # 候选帧率要同时满足「至少 3 帧」和「至少占全片 0.1%」：前者过滤单包
    # 毛刺，后者防止几帧 60fps 把整片从 24fps 抬到 60fps。低于门槛的帧
    # 不算候选，但仍计入会被丢弃的帧数。
    threshold = max(MIN_RATE_SAMPLES, round(total * MIN_RATE_SHARE))
    budget = max(0.0, min(1.0, float(max_drop_ratio))) * total
    chosen = None
    highest = 0.0
    dropped = 0
    groups = _rate_clusters(counts)
    # 从高帧率（短时长）往低帧率走。dropped 是当前候选之上、已经确定
    # 会被丢掉的帧数；只有没超预算的候选才配当目标。
    for mean_us, count in groups:
        if count >= threshold:
            if chosen is None:
                highest = 1_000_000 / mean_us
            if dropped <= budget:
                chosen = mean_us
            else:
                break
        dropped += count
        if chosen is not None and dropped > budget:
            break
    if chosen is None:
        return 0.0, 0.0, 0.0
    dropped = sum(n for mean_us, n in groups if mean_us < chosen)
    return 1_000_000 / chosen, highest, dropped / total


def _probe_rate_plan(ffprobe: str, path: Path,
                     max_drop_ratio: float) -> tuple:
    """扫全片 packet 时长，返回 `(目标帧率, 最高帧率, 丢弃比例)`。

    只对 VFR 片源做。通读一遍容器层（7.5GB 实测约一分钟），相对几小时的
    编码可以忽略；抽样几个窗口则可能正好漏掉一段 60fps，压出来就丢帧了。
    失败返回 `(0, 0, 0)`，调用方会跳过这个文件。
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
        return 0.0, 0.0, 0.0
    return _rate_plan_from_durations(out.splitlines(), max_drop_ratio)


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


def primary_video_stream(streams: list) -> dict:
    """第一条真正的视频流：跳过 mp4 封面图（attached_pic）。

    ``0:V`` / ``0:V:0`` 会把 attached_pic 排除在外，probe 必须用同一条流，
    否则封面排在前面时拿封面当正片，宽高、帧率、HDR 全错。
    """
    for stream in streams or ():
        if stream.get("codec_type") != "video":
            continue
        disposition = stream.get("disposition") or {}
        if to_int(disposition.get("attached_pic")) == 1:
            continue
        return stream
    return {}


def probe(tools: Tools, path: Path,
          vfr_max_drop_ratio: float = 0.05) -> VideoInfo:
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
    vstream = primary_video_stream(streams)
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
        # 不能按标称 24.75 压：30fps 段会被悄悄抽帧。目标帧率从全片 packet
        # 时长里选，默认只允许丢掉 5% 以内的高帧片段；30fps 占多数的片源
        # 仍会选 30fps，不会被标称帧率带偏。
        target, highest, dropped = _probe_rate_plan(
            tools.ffprobe, path, vfr_max_drop_ratio)
        info.vfr_max_fps = highest
        info.vfr_drop_ratio = dropped
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
    # 只取主视频流的帧：封面图也可能出现在 frames 里，拿它的 SEI 会把 HDR
    # 元数据认错。stream_index 缺失时退回第一条视频帧（老构建的兼容路径）。
    vindex = vstream.get("index")
    vframe = next((f for f in data.get("frames") or []
                   if f.get("media_type") == "video"
                   and (vindex is None or f.get("stream_index") == vindex)), {})
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
    info.audio_profiles = [str(s.get("profile") or "") for s in astreams]
    info.subtitle_codecs = [s.get("codec_name", "") for s in sstreams]
    return info


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def should_skip(info: VideoInfo, cfg: dict, check_bpp: bool = True):
    """按元数据、交错、VFR、大小、BPP、收益粗估判断要不要处理。

    ``check_bpp=False`` 给 BPP 预检模式用：先绕过 BPP，确认其余条件是否
    已经足以跳过（比如 AV1 源的 expected_ratio 本来就判没收益）。
    """
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

    # 探测不出 VFR 的目标帧率时不能硬压：ffmpeg 会按标称帧率做 CFR，
    # 高帧率段直接被抽帧，而 VMAF 未必看得出来（它比的也是抽过帧的片段）。
    if info.vfr and info.target_fps <= 0:
        return True, VFR_UNRESOLVED_REASON

    min_size = int(cfg["skip"].get("min_size_mb", 0)) * 1024 * 1024
    if info.size and info.size < min_size:
        return True, f"文件太小（{info.size // 1024 // 1024} MB）"

    bpp_cfg = cfg["skip"]["bpp"]
    if check_bpp and info.codec_family and info.codec_family in bpp_cfg:
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
                    f"{info.codec_family} {BPP_REASON_MARKER} {bpp:.4f} bits，"
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
