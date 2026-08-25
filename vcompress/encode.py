"""编码参数生成、编码执行、抽样与自适应 CRF 搜索。"""

import collections
import json
import re
import shlex
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import proc
from .util import to_float, to_int

_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")
_DEPTH_RE = re.compile(r"(\d{1,2})(?:[lb]e)?$")

STDERR_TAIL_LINES = 200


class EncodeError(RuntimeError):
    pass


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


# -------------------------------------------------------------------- 音频/字幕

LOSSLESS_AUDIO = ("pcm_", "flac", "alac", "ape", "wavpack", "truehd", "mlp")
OPUS_MIN_KBPS = 48
OPUS_MAX_KBPS = 510
# libopus 只认这几种规范布局，5.1(side) 这类变体会让编码器打不开
OPUS_LAYOUTS = {1: "mono", 2: "stereo", 3: "3.0", 4: "quad",
                5: "5.0", 6: "5.1", 7: "6.1", 8: "7.1"}


def opus_bitrate(base_kbps: int, channels: int) -> int:
    """opus_kbps 以立体声为基准，按声道数线性缩放。"""
    ch = channels if channels > 0 else 2
    return max(OPUS_MIN_KBPS, min(OPUS_MAX_KBPS, round(base_kbps * ch / 2)))


def opus_layout_args(index: int, channels: int) -> list:
    """把声道布局规范化到 libopus 接受的形式。

    ffmpeg 的 libopus 封装会校验布局名：5.1(side)、unknown 这类直接报
    "Invalid channel layout ... for specified mapping family" 而整个转码失败。
    声道数已知就按规范布局重排，超过 8 声道退回任意映射。
    """
    layout = OPUS_LAYOUTS.get(channels)
    if layout:
        return [f"-filter:a:{index}", f"aformat=channel_layouts={layout}"]
    return [f"-mapping_family:a:{index}", "255"]


def audio_plan(info, cfg) -> list:
    """逐条音轨的处置决定：("copy"|"opus", 码率 bps)，码率 0 表示不知道。

    单独抽出来是为了让「产物会有多大」这个预估和实际编码参数用同一套决定，
    不会两边各写一份判断然后慢慢走偏。
    """
    threshold = int(cfg["audio"].get("transcode_over_kbps", 512)) * 1000
    base_kbps = int(cfg["audio"].get("opus_kbps", 160))
    plan = []
    for i, codec in enumerate(info.audio_codecs):
        codec = (codec or "").lower()
        br = info.audio_bitrates[i] if i < len(info.audio_bitrates) else 0
        ch = info.audio_channels[i] if i < len(info.audio_channels) else 2
        if codec.startswith(LOSSLESS_AUDIO) or (br and br > threshold):
            plan.append(("opus", opus_bitrate(base_kbps, ch) * 1000))
        else:
            plan.append(("copy", br))
    return plan


def predicted_size(info, cfg, video_bitrate: int):
    """按样本实测的视频码率预估产物体积；信息不全时返回 None。

    比 [skip].expected_ratio 那张按编码器写死的粗估准得多：那是"h264 大概能
    压到 55%"，这是"这个片源在选定 CRF 下实测每秒多少字节"。字幕/章节/附件
    的开销忽略不计，所以估出来偏小——宁可偏乐观，也不要把本来能省下来的文件
    误判成不值得压。
    """
    if video_bitrate <= 0 or info.duration <= 0:
        return None
    total = video_bitrate
    for _kind, br in audio_plan(info, cfg):
        if br <= 0:
            return None  # 有音轨码率不明，整个预估就不可信，宁可不猜
        total += br
    return int(total * info.duration / 8)


def audio_args(info, cfg) -> list:
    """逐条音轨决定 copy 还是转 Opus，不因为一条无损轨就重压所有音轨。"""
    if not info.audio_codecs:
        return []
    args = []
    transcoding = False
    for i, (kind, bitrate) in enumerate(audio_plan(info, cfg)):
        if kind == "opus":
            ch = info.audio_channels[i] if i < len(info.audio_channels) else 2
            args += [f"-c:a:{i}", "libopus",
                     f"-b:a:{i}", f"{bitrate // 1000}k"]
            args += opus_layout_args(i, ch)
            transcoding = True
        else:
            args += [f"-c:a:{i}", "copy"]
    if not transcoding:
        return ["-c:a", "copy"]
    return args


MKV_LIKE = {".mkv", ".webm"}
MP4_LIKE = {".mp4", ".m4v", ".mov"}
# matroska 不认 mp4 系的文本字幕，mp4 只认 mov_text，跨容器复制会直接失败
MP4_TEXT_SUBS = {"mov_text", "tx3g"}
MKV_TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "webvtt", "text", "subviewer"}


def subtitle_args(info, container: str) -> list:
    """逐条字幕决定 copy 还是按目标容器转换文本字幕。"""
    if not info.subtitle_codecs:
        return []
    container = (container or "").lower()
    args = []
    for i, codec in enumerate(info.subtitle_codecs):
        codec = (codec or "").lower()
        if container in MKV_LIKE and codec in MP4_TEXT_SUBS:
            args += [f"-c:s:{i}", "srt"]
        elif container in MP4_LIKE and codec in MKV_TEXT_SUBS:
            args += [f"-c:s:{i}", "mov_text"]
        else:
            args += [f"-c:s:{i}", "copy"]
    return args


# ------------------------------------------------------------------ 命令行拼装

def video_args(encoder_cfg: dict, crf: int) -> list:
    args = [
        "-c:v", encoder_cfg["codec"],
        "-preset", str(encoder_cfg["preset"]),
        "-crf", str(crf),
    ]
    pix_fmt = encoder_cfg.get("pix_fmt")
    if pix_fmt:
        args += ["-pix_fmt", pix_fmt]
    extra = encoder_cfg.get("extra", "")
    if extra:
        args.extend(shlex.split(extra))
    return args


def merge_comment(original: str, marker: str) -> str:
    """把标记追加到原有 comment 后面，而不是把人家的注释盖掉。

    `-map_metadata 0` 会把原始 comment 带过来，紧接着的 `-metadata comment=`
    是整条覆盖，所以要自己拼。已经含标记就不重复追加。
    """
    original = (original or "").strip()
    if not original:
        return marker
    if marker.lower() in original.lower():
        return original
    return f"{original} | {marker}"


def color_args(info) -> list:
    """把源的色彩属性显式写给编码器。

    新一点的 ffmpeg 会自动把这些属性从解码器传到编码器（本机 ffmpeg 9 实测
    四项和 HDR10 母版信息都能自动带过去），但这依赖构建版本，而丢了它们的后果
    是 HDR 片源被当成 SDR 播——画面数值一模一样，VMAF 测不出来。显式写一遍是
    零成本的保险；源没标的项不写，免得凭空造出错误的标记。
    """
    args = []
    for attr, flag in (("color_primaries", "-color_primaries"),
                       ("color_transfer", "-color_trc"),
                       ("color_space", "-colorspace"),
                       ("color_range", "-color_range")):
        value = getattr(info, attr, "") or ""
        if value:
            args += [flag, value]
    return args


def build_cmd(ffmpeg: str, src: Path, dst: Path, encoder_cfg: dict,
              crf: int, a_args: list, s_args: list, marker: str,
              original_comment: str = "", c_args: list = None) -> list:
    container = dst.suffix.lower()
    maps = ["-map", "0:V", "-map", "0:a?", "-map", "0:s?"]
    tail = []
    if container in MKV_LIKE:
        # 附件只有 matroska 支持；data 流（如 mp4 的 tmcd 时间码）装不进去，
        # 硬塞会让整个转码失败，只能显式丢弃。
        maps += ["-map", "0:t?"]
        tail += ["-c:t", "copy", "-dn"]
    else:
        maps += ["-map", "0:d?"]
        tail += ["-c:d", "copy"]
    if container in MP4_LIKE:
        tail += ["-movflags", "+use_metadata_tags"]
    return [
        ffmpeg, "-hide_banner", "-y",
        "-i", str(src),
        *maps,
        "-map_metadata", "0",
        "-map_chapters", "0",
        *video_args(encoder_cfg, crf),
        *(c_args or []),
        *a_args,
        *s_args,
        *tail,
        "-metadata", f"comment={merge_comment(original_comment, marker)}",
        "-progress", "pipe:1", "-nostats",
        str(dst),
    ]


# ------------------------------------------------------------------ 编码与进度

# ffmpeg 每刷新一次进度会同时发 out_time_us、out_time_ms、out_time 三个键，
# 认多个键就会让同一个时刻回调多次（进度条重画、速度平滑被污染）。
TIME_KEYS = ("out_time_us", "out_time_ms")


def _parse_progress_line(line: str):
    """解析 -progress 的一行，返回 (键名, 秒) 或 None。

    out_time_ms 名字骗人：ffmpeg 往里写的其实也是微秒。
    """
    if "=" not in line:
        return None
    key, _, value = line.partition("=")
    if key not in TIME_KEYS:
        return None
    try:
        return key, float(value.strip()) / 1e6
    except ValueError:
        return None


def _parse_speed(line: str):
    if not line.startswith("speed="):
        return None
    value = line.partition("=")[2].strip().rstrip("x")
    try:
        return float(value)
    except ValueError:
        return None


def _drain(stream, sink) -> None:
    """把子进程 stderr 持续读走，防止管道写满后双向阻塞。"""
    try:
        for line in stream:
            sink.append(line)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def run_encode(cmd: list, total_duration: float, progress_cb=None) -> None:
    process = proc.spawn(cmd)
    # ffmpeg 的进度走 stdout，日志走 stderr；stderr 必须并行读，否则
    # 告警较多的片源写满 64KB 管道后 ffmpeg 和本进程互相等死。
    err_tail = collections.deque(maxlen=STDERR_TAIL_LINES)
    err_thread = threading.Thread(target=_drain, args=(process.stderr, err_tail),
                                  daemon=True)
    err_thread.start()
    start = time.monotonic()
    last_time = start
    last_pos = 0.0
    last_speed = 0.0
    time_key = None
    try:
        for raw in process.stdout:
            parsed = _parse_progress_line(raw.strip())
            if parsed is None:
                continue
            key, pos = parsed
            # 锁定第一个见到的时间键，后面只认它，一次刷新只回调一次
            if time_key is None:
                time_key = key
            elif key != time_key:
                continue
            now = time.monotonic()
            if pos > last_pos and now > last_time:
                inst = (pos - last_pos) / (now - last_time)
                last_speed = 0.5 * last_speed + 0.5 * inst
            last_time = now
            last_pos = pos
            if progress_cb:
                if total_duration > 0:
                    pct = min(99.9, pos / total_duration * 100)
                    remaining = max(0.0, total_duration - pos)
                    eta = remaining / last_speed if last_speed > 0 else 0.0
                    progress_cb(pct, eta, last_speed)
                else:
                    progress_cb(None, pos, last_speed)
        rc = process.wait()
    except BaseException:
        try:
            process.kill()
        except OSError:
            pass
        raise
    finally:
        proc.unregister(process)
        err_thread.join(timeout=5)
        try:
            process.stdout.close()
        except OSError:
            pass
    if rc != 0:
        tail = "".join(err_tail).strip()[-500:]
        raise EncodeError(f"ffmpeg exited {rc}: {tail}")


def _run_quiet(cmd: list) -> tuple:
    rc, _out, err = proc.run_managed(cmd)
    return rc, err


# ------------------------------------------------------------------ 抽样与 VMAF

SEEK_LEADIN_S = 2.0


def frame_aligned_start(start: float, fps: float) -> float:
    """把切点挪到两帧中间，切出来的片段才不会在两个文件里错开一帧。

    同一帧的时间戳在不同容器里精度不同：mp4 常用 1/24000，matroska 只有
    1/1000，23.976fps 下一帧的 pts 两边能差近 1ms。切点正好压在帧边界上时，
    一边把这帧算进片段、另一边算在片段外，参考段和待测段就整体错开一帧，
    libvmaf 逐帧比出来的分数会从 91 掉到 33——画质没问题，是量错了。
    切在帧间隙正中间，±1ms 的抖动再也翻不过边界。
    """
    if fps <= 0 or start <= 0:
        return max(0.0, start)
    step = 1.0 / fps
    frame = round(start / step)
    return max(0.0, (frame - 0.5) * step)


def lossless_args(codec: str = "libx264") -> list:
    """无损抽样片段的编码参数。ffv1 没有 crf 概念，本身就是无损。"""
    if codec == "ffv1":
        return ["-c:v", "ffv1", "-level", "1"]
    return ["-c:v", codec, "-crf", "0", "-preset", "ultrafast"]


def segment_cmd(ffmpeg: str, src: Path, start: float, seconds: float,
                out: Path, codec: str = "libx264") -> list:
    """抽一段无损片段的命令：先粗 seek 到目标前几秒，再精确 seek 微调。

    只用精确 seek（-ss 放在 -i 之后）要从文件开头解码，两小时的片子抽三段
    就是三次近全片解码；只用快速 seek 会停在关键帧上，参考片段和待测片段
    的关键帧位置不同就会错位，VMAF 直接失去意义。两段式两个问题都避开。

    用 0:V:0 而不是 0:v:0：v 会把封面图（attached_pic）算成视频流，真编码
    时用的是 0:V（排除封面），两边取的流必须是同一条才有比较意义。
    """
    lead = min(SEEK_LEADIN_S, max(0.0, start))
    return [
        ffmpeg, "-hide_banner", "-y",
        "-ss", f"{max(0.0, start - lead):.3f}",
        "-i", str(src),
        "-ss", f"{lead:.3f}", "-t", f"{seconds:.3f}",
        "-map", "0:V:0", "-an", "-sn", "-dn",
        *lossless_args(codec),
        str(out),
    ]


def extract_segment(ffmpeg: str, src: Path, start: float, seconds: float,
                    out: Path, codec: str = "libx264"):
    rc, _out, err = proc.run_managed(
        segment_cmd(ffmpeg, src, start, seconds, out, codec))
    if rc != 0:
        raise EncodeError(f"segment extract failed: {err.strip()[-300:]}")


def encode_sample(ffmpeg: str, ref: Path, crf: int, encoder_cfg: dict, out: Path):
    cmd = [
        ffmpeg, "-hide_banner", "-y",
        "-i", str(ref),
        *video_args(encoder_cfg, crf),
        "-an",
        str(out),
    ]
    rc, err = _run_quiet(cmd)
    if rc != 0:
        raise EncodeError(f"sample encode failed at crf={crf}: {err.strip()[-300:]}")


# Netflix 只发布了 1080p 和 4K 两套模型。默认模型按「1080p 屏幕、3 倍屏高
# 观看距离」训练，用在 4K 片源上是错的量具；4K 模型对应「4K 屏幕、1.5 倍屏高」。
# 同样的失真在 4K 模型下分数更高（实测同一对文件 95.0 -> 97.4），因为那个观看
# 条件下本来就更不容易看出来。1440p 这类中间分辨率仍用默认模型：它离 1080p 的
# 观看条件更近。
VMAF_MODEL_4K = "vmaf_4k_v0.6.1"


def vmaf_model_for(width: int, height: int, enabled: bool = True) -> str:
    """按分辨率挑 VMAF 模型；返回空串表示用 libvmaf 的默认模型。"""
    if not enabled:
        return ""
    if to_int(width) >= 3000 or to_int(height) >= 1700:
        return VMAF_MODEL_4K
    return ""


def _pooled_score(data: dict, pool: str = "harmonic_mean") -> float:
    """从 libvmaf 的 json 里取分数，结构不对时报明确错误。

    默认取调和平均而不是算术平均：算术平均会被大量好帧稀释，一段里有几十帧
    崩到 60 分，均值还能是 94。调和平均对低分帧更敏感，更贴近"最差处也得看得
    过去"这个目标。libvmaf 版本老到没有这个键时退回算术平均。
    """
    pooled = data.get("pooled_metrics") or {}
    entry = pooled.get("vmaf")
    if not isinstance(entry, dict):
        # 退而求其次也只认名字里带 vmaf 的项；pooled_metrics 里还有一堆特征
        # 分数（integer_adm2 之类），按字典顺序乱取会拿到毫无意义的数
        entry = next((v for k, v in pooled.items()
                      if "vmaf" in k.lower() and isinstance(v, dict)), None)
    if not isinstance(entry, dict):
        entry = next((v for v in pooled.values() if isinstance(v, dict)), {})
    for key in (pool, "mean"):
        if key not in entry:
            continue
        try:
            return float(entry[key])
        except (TypeError, ValueError) as e:
            raise EncodeError(
                f"libvmaf {key} score is not a number: {entry[key]!r}") from e
    raise EncodeError("libvmaf output has no pooled score")


def vmaf_lavfi(shift: int = 0, model: str = "") -> str:
    """libvmaf 滤镜链：两路都按帧号重编时间戳，逐帧对齐后再比。

    framesync 按时间戳配对，两个片段的 pts 网格哪怕整体差 1ms 都会错配；
    setpts=N/FR/TB 把两路都改成同一套帧号时间戳，配对只看第几帧。
    shift>0 丢掉待测段开头的帧，shift<0 丢掉参考段开头的帧，用来验证
    「分数低是不是因为错了一帧」。

    model 给定时切换 VMAF 模型（默认模型按 1080p 观看条件训练）。
    """
    d_trim = f"trim=start_frame={shift}," if shift > 0 else ""
    r_trim = f"trim=start_frame={-shift}," if shift < 0 else ""
    opts = "log_path=vmaf.json:log_fmt=json"
    if model:
        opts += f":model=version={model}"
    return (
        f"[0:v]{d_trim}setpts=N/FR/TB[d];"
        f"[1:v]{r_trim}setpts=N/FR/TB[r];"
        f"[d][r]libvmaf={opts}"
    )


def vmaf_score(ffmpeg: str, distorted: Path, reference: Path, shift: int = 0,
               model: str = "", pool: str = "harmonic_mean", log=None):
    """量一对片段的 VMAF；指定的模型加载不了时退回默认模型。

    退回这一步是必要的：模型名不被认识时 libvmaf 直接报错退出，而校验链把
    「libvmaf 用不了」当成告警放过——4K 片源会因此全部跳过画质门槛。
    """
    try:
        return _vmaf_run(ffmpeg, distorted, reference, shift, model, pool)
    except EncodeError:
        if not model:
            raise
        if log:
            log.warning(f"libvmaf model {model} is unavailable in this build; "
                        f"falling back to the default model")
        return _vmaf_run(ffmpeg, distorted, reference, shift, "", pool)


def _vmaf_run(ffmpeg: str, distorted: Path, reference: Path, shift: int,
              model: str, pool: str):
    with tempfile.TemporaryDirectory(prefix="vcvmaf_") as td:
        log_path = Path(td) / "vmaf.json"
        cmd = [
            ffmpeg, "-hide_banner",
            "-i", str(distorted),
            "-i", str(reference),
            "-lavfi", vmaf_lavfi(shift, model),
            "-f", "null", "-",
        ]
        rc, _out, err = proc.run_managed(cmd, cwd=td)
        if rc != 0:
            raise EncodeError(f"libvmaf failed: {err.strip()[-300:]}")
        try:
            data = json.loads(log_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise EncodeError(f"cannot read libvmaf output: {e}") from e
        return _pooled_score(data, pool)


def sample_starts(duration: float, seconds: float, count: int) -> list:
    """把 count 段样本均匀铺在整片上，返回每段的起点。

    只取中间一段是不够的：一部片子的中点可能是明亮的动作戏，也可能是全黑的
    过场，按它定的 CRF 拿到暗场或高动态段就不成立了。均匀铺开并按最差段决策，
    才和后面用多段抽检的校验是同一个口径。
    """
    count = max(1, count)
    if duration <= 0:
        return [0.0]
    span = min(seconds, duration)
    starts = []
    for i in range(count):
        frac = (i + 0.5) / count
        start = max(0.0, min(duration - span, duration * frac - span / 2))
        starts.append(start)
    # 片子太短时几段会挤在一起，去重免得白编同一段
    unique = []
    for start in starts:
        if all(abs(start - kept) > 0.25 for kept in unique):
            unique.append(start)
    return unique


@dataclass(frozen=True)
class CrfChoice:
    """自适应搜索的结论。

    除了选中的 CRF，还带上样本实测的视频码率和是否真的达到了目标画质——这两个
    信息能在编码开始前就判断「这文件值不值得压几个小时」。
    """

    crf: int
    video_bitrate: int = 0
    meets_target: bool = True
    worst_vmaf: float = 0.0


def _probe_crf(tools, refs: list, crf: int, enc_cfg: dict, root: Path,
               target: float, model: str, log) -> tuple:
    """在所有样本段上试这个 CRF，返回 (最差段分数, 样本码率 bps)。

    取最差而不是平均：平均分能被好段抬起来，而观感是由最差处决定的。某段已经
    不达标时后面的段就不用再量了，直接判这个 CRF 不行。
    """
    worst = None
    total_bytes = 0
    measured = 0
    for i, ref in enumerate(refs):
        sample = root / f"s{crf}_{i}.mkv"
        try:
            encode_sample(tools.ffmpeg, ref, crf, enc_cfg, sample)
            size = sample.stat().st_size
            score = vmaf_score(tools.ffmpeg, sample, ref, model=model, log=log)
        finally:
            sample.unlink(missing_ok=True)
        total_bytes += size
        measured += 1
        log.debug(f"adaptive probe: crf={crf} segment {i + 1}/{len(refs)} "
                  f"vmaf={score:.2f} {size} bytes")
        worst = score if worst is None else min(worst, score)
        if worst < target:
            break
    return (worst if worst is not None else 0.0), total_bytes, measured


def pick_crf_adaptive(tools, src: Path, duration: float,
                      enc_cfg: dict, ad_cfg: dict, log,
                      lossless_codec: str = "libx264",
                      model: str = "") -> "CrfChoice | None":
    target = to_float(ad_cfg.get("target_vmaf"), 95.0)
    lo = int(ad_cfg.get("crf_min", 16))
    hi = int(ad_cfg.get("crf_max", 44))
    step = max(1, int(ad_cfg.get("search_step", 2)))
    seconds = to_float(ad_cfg.get("sample_seconds"), 10.0)
    count = max(1, to_int(ad_cfg.get("sample_count"), 4))

    starts = sample_starts(duration, seconds, count)
    span = min(seconds, duration) if duration > 0 else seconds
    best = None
    log.info(f"adaptive search: extracting {len(starts)} reference segment(s) "
             f"of {seconds:.0f}s from {src.name}")
    tmpdir = tempfile.TemporaryDirectory(prefix="vcadapt_")
    try:
        root = Path(tmpdir.name)
        refs = []
        for i, start in enumerate(starts):
            ref = root / f"ref{i}.mkv"
            extract_segment(tools.ffmpeg, src, start, span, ref,
                            lossless_codec)
            refs.append(ref)

        while hi - lo > step:
            mid = (lo + hi) // 2
            score, sample_bytes, measured = _probe_crf(
                tools, refs, mid, enc_cfg, root, target, model, log)
            log.info(f"adaptive probe: crf={mid} worst-segment vmaf="
                     f"{score:.2f} (target {target})")
            if score >= target:
                best = CrfChoice(mid, _sample_bitrate(sample_bytes, span,
                                                      measured), True, score)
                lo = mid
            else:
                hi = mid
        if best is None:
            score, sample_bytes, measured = _probe_crf(
                tools, refs, lo, enc_cfg, root, target, model, log)
            if score < target:
                log.warning(
                    f"even crf={lo} only reaches vmaf={score:.2f} "
                    f"< {target}"
                )
            best = CrfChoice(lo, _sample_bitrate(sample_bytes, span, measured),
                             score >= target, score)
        log.info(f"adaptive crf selected: {best.crf} (target vmaf {target}, "
                 f"worst of {len(refs)} segment(s) {best.worst_vmaf:.2f}, "
                 f"sampled {best.video_bitrate // 1000} kbps)")
        return best
    except (EncodeError, OSError) as e:
        log.warning(f"adaptive crf search failed ({e}); falling back to default")
        return None
    finally:
        tmpdir.cleanup()


def _sample_bitrate(total_bytes: int, span: float, segments: int) -> int:
    """样本总字节换算成码率。样本是均匀铺开抽的，可以代表全片平均。"""
    seconds = span * segments
    if seconds <= 0 or total_bytes <= 0:
        return 0
    return int(total_bytes * 8 / seconds)
