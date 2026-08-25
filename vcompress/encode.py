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
from .util import to_float

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


def audio_args(info, cfg) -> list:
    """逐条音轨决定 copy 还是转 Opus，不因为一条无损轨就重压所有音轨。"""
    if not info.audio_codecs:
        return []
    threshold = int(cfg["audio"].get("transcode_over_kbps", 512)) * 1000
    base_kbps = int(cfg["audio"].get("opus_kbps", 160))
    args = []
    transcoding = False
    for i, codec in enumerate(info.audio_codecs):
        codec = (codec or "").lower()
        br = info.audio_bitrates[i] if i < len(info.audio_bitrates) else 0
        ch = info.audio_channels[i] if i < len(info.audio_channels) else 2
        if codec.startswith(LOSSLESS_AUDIO) or (br and br > threshold):
            args += [f"-c:a:{i}", "libopus",
                     f"-b:a:{i}", f"{opus_bitrate(base_kbps, ch)}k"]
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


def _pooled_mean(data: dict) -> float:
    """从 libvmaf 的 json 里取平均分，结构不对时报明确错误。"""
    pooled = data.get("pooled_metrics") or {}
    entry = pooled.get("vmaf")
    if not isinstance(entry, dict):
        entry = next((v for v in pooled.values() if isinstance(v, dict)), {})
    mean = entry.get("mean")
    if mean is None:
        raise EncodeError("libvmaf output has no pooled mean score")
    try:
        return float(mean)
    except (TypeError, ValueError) as e:
        raise EncodeError(f"libvmaf mean score is not a number: {mean!r}") from e


def vmaf_lavfi(shift: int = 0) -> str:
    """libvmaf 滤镜链：两路都按帧号重编时间戳，逐帧对齐后再比。

    framesync 按时间戳配对，两个片段的 pts 网格哪怕整体差 1ms 都会错配；
    setpts=N/FR/TB 把两路都改成同一套帧号时间戳，配对只看第几帧。
    shift>0 丢掉待测段开头的帧，shift<0 丢掉参考段开头的帧，用来验证
    「分数低是不是因为错了一帧」。
    """
    d_trim = f"trim=start_frame={shift}," if shift > 0 else ""
    r_trim = f"trim=start_frame={-shift}," if shift < 0 else ""
    return (
        f"[0:v]{d_trim}setpts=N/FR/TB[d];"
        f"[1:v]{r_trim}setpts=N/FR/TB[r];"
        f"[d][r]libvmaf=log_path=vmaf.json:log_fmt=json"
    )


def vmaf_score(ffmpeg: str, distorted: Path, reference: Path, shift: int = 0):
    with tempfile.TemporaryDirectory(prefix="vcvmaf_") as td:
        log_path = Path(td) / "vmaf.json"
        cmd = [
            ffmpeg, "-hide_banner",
            "-i", str(distorted),
            "-i", str(reference),
            "-lavfi", vmaf_lavfi(shift),
            "-f", "null", "-",
        ]
        rc, _out, err = proc.run_managed(cmd, cwd=td)
        if rc != 0:
            raise EncodeError(f"libvmaf failed: {err.strip()[-300:]}")
        try:
            data = json.loads(log_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise EncodeError(f"cannot read libvmaf output: {e}") from e
        return _pooled_mean(data)


def pick_crf_adaptive(tools, src: Path, duration: float,
                      enc_cfg: dict, ad_cfg: dict, log,
                      lossless_codec: str = "libx264") -> int | None:
    target = to_float(ad_cfg.get("target_vmaf"), 95.0)
    lo = int(ad_cfg.get("crf_min", 16))
    hi = int(ad_cfg.get("crf_max", 44))
    step = max(1, int(ad_cfg.get("search_step", 2)))
    seconds = to_float(ad_cfg.get("sample_seconds"), 30.0)

    center = max(0.0, duration / 2 - seconds / 2)
    best = None
    log.info(f"adaptive search: extracting reference segment "
             f"({seconds:.0f}s around mid-point of {src.name})")
    tmpdir = tempfile.TemporaryDirectory(prefix="vcadapt_")
    try:
        root = Path(tmpdir.name)
        ref = root / "ref.mkv"
        extract_segment(tools.ffmpeg, src, center, min(seconds, duration), ref,
                        lossless_codec)

        while hi - lo > step:
            mid = (lo + hi) // 2
            sample = root / f"s{mid}.mkv"
            encode_sample(tools.ffmpeg, ref, mid, enc_cfg, sample)
            score = vmaf_score(tools.ffmpeg, sample, ref)
            log.info(f"adaptive probe: crf={mid} vmaf={score:.2f} "
                     f"(target {target})")
            if score >= target:
                best = mid
                lo = mid
            else:
                hi = mid
        if best is None:
            sample = root / "smin.mkv"
            encode_sample(tools.ffmpeg, ref, lo, enc_cfg, sample)
            score = vmaf_score(tools.ffmpeg, sample, ref)
            if score < target:
                log.warning(
                    f"even crf={lo} only reaches vmaf={score:.2f} "
                    f"< {target}; using crf={lo}"
                )
            best = lo
        log.info(f"adaptive crf selected: {best} (target vmaf {target})")
        return best
    except (EncodeError, OSError) as e:
        log.warning(f"adaptive crf search failed ({e}); falling back to default")
        return None
    finally:
        tmpdir.cleanup()
