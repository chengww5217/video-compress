import collections
import json
import re
import shlex
import subprocess
import tempfile
import threading
import time
from pathlib import Path

_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")

_active_procs = set()
_lock = threading.Lock()
_cancelled = threading.Event()

STDERR_TAIL_LINES = 200


class EncodeError(RuntimeError):
    pass


def register(proc):
    with _lock:
        _active_procs.add(proc)


def unregister(proc):
    with _lock:
        _active_procs.discard(proc)


def terminate_all():
    """立即杀掉所有在跑的 ffmpeg，并置上取消标志。"""
    _cancelled.set()
    with _lock:
        procs = list(_active_procs)
    for p in procs:
        try:
            p.kill()
        except OSError:
            pass


def is_cancelled() -> bool:
    return _cancelled.is_set()


def clear_cancel():
    _cancelled.clear()


def run_managed(cmd: list, cwd=None) -> tuple:
    """跑一个短命 ffmpeg 子进程，注册后可被 terminate_all 立刻杀掉。"""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
    )
    register(proc)
    try:
        out, err = proc.communicate()
    except BaseException:
        try:
            proc.kill()
        except OSError:
            pass
        proc.communicate()
        raise
    finally:
        unregister(proc)
    return proc.returncode, out, err


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
        rc, out, _err = run_managed([ffmpeg, "-hide_banner", "-encoders"])
    except OSError:
        return set()
    return _parse_encoder_names(out) if rc == 0 else set()


def available_filters(ffmpeg: str) -> set:
    try:
        rc, out, _err = run_managed([ffmpeg, "-hide_banner", "-filters"])
    except OSError:
        return set()
    return _parse_filter_names(out) if rc == 0 else set()


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


def video_args(encoder_cfg: dict, crf: int) -> list:
    args = [
        "-c:v", encoder_cfg["codec"],
        "-preset", str(encoder_cfg["preset"]),
        "-crf", str(crf),
    ]
    extra = encoder_cfg.get("extra", "")
    if extra:
        args.extend(shlex.split(extra))
    return args


def build_cmd(ffmpeg: str, src: Path, dst: Path, encoder_cfg: dict,
              crf: int, a_args: list, s_args: list, marker: str) -> list:
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
        *a_args,
        *s_args,
        *tail,
        "-metadata", f"comment={marker}",
        "-progress", "pipe:1", "-nostats",
        str(dst),
    ]


def _parse_progress_line(line: str):
    if "=" not in line:
        return None
    key, _, value = line.partition("=")
    value = value.strip()
    if key in ("out_time_us", "out_time_ms"):
        try:
            return float(value) / 1e6
        except ValueError:
            return None
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
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    register(proc)
    # ffmpeg 的进度走 stdout，日志走 stderr；stderr 必须并行读，否则
    # 告警较多的片源写满 64KB 管道后 ffmpeg 和本进程互相等死。
    err_tail = collections.deque(maxlen=STDERR_TAIL_LINES)
    err_thread = threading.Thread(target=_drain, args=(proc.stderr, err_tail),
                                  daemon=True)
    err_thread.start()
    start = time.monotonic()
    last_time = start
    last_pos = 0.0
    last_speed = 0.0
    try:
        for raw in proc.stdout:
            line = raw.strip()
            pos = _parse_progress_line(line)
            if pos is None:
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
        rc = proc.wait()
    except BaseException:
        try:
            proc.kill()
        except OSError:
            pass
        raise
    finally:
        unregister(proc)
        err_thread.join(timeout=5)
        try:
            proc.stdout.close()
        except OSError:
            pass
    if rc != 0:
        tail = "".join(err_tail).strip()[-500:]
        raise EncodeError(f"ffmpeg exited {rc}: {tail}")


def _run_quiet(cmd: list) -> tuple:
    rc, _out, err = run_managed(cmd)
    return rc, err


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


def segment_cmd(ffmpeg: str, src: Path, start: float, seconds: float,
                out: Path) -> list:
    """抽一段无损片段的命令：先粗 seek 到目标前几秒，再精确 seek 微调。

    只用精确 seek（-ss 放在 -i 之后）要从文件开头解码，两小时的片子抽三段
    就是三次近全片解码；只用快速 seek 会停在关键帧上，参考片段和待测片段
    的关键帧位置不同就会错位，VMAF 直接失去意义。两段式两个问题都避开。
    """
    lead = min(SEEK_LEADIN_S, max(0.0, start))
    return [
        ffmpeg, "-hide_banner", "-y",
        "-ss", f"{max(0.0, start - lead):.3f}",
        "-i", str(src),
        "-ss", f"{lead:.3f}", "-t", f"{seconds:.3f}",
        "-map", "0:v:0", "-an", "-sn", "-dn",
        "-c:v", "libx264", "-crf", "0", "-preset", "ultrafast",
        str(out),
    ]


def extract_segment(ffmpeg: str, src: Path, start: float, seconds: float,
                    out: Path):
    rc, _out, err = run_managed(segment_cmd(ffmpeg, src, start, seconds, out))
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
        rc, _out, err = run_managed(cmd, cwd=td)
        if rc != 0:
            raise EncodeError(f"libvmaf failed: {err.strip()[-300:]}")
        try:
            data = json.loads(log_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise EncodeError(f"cannot read libvmaf output: {e}") from e
        return _pooled_mean(data)


def pick_crf_adaptive(tools, src: Path, duration: float,
                      enc_cfg: dict, ad_cfg: dict, log) -> int | None:
    target = float(ad_cfg.get("target_vmaf", 95.0))
    lo = int(ad_cfg.get("crf_min", 16))
    hi = int(ad_cfg.get("crf_max", 44))
    step = max(1, int(ad_cfg.get("search_step", 2)))
    seconds = float(ad_cfg.get("sample_seconds", 30))

    center = max(0.0, duration / 2 - seconds / 2)
    best = None
    log.info(f"adaptive search: extracting reference segment "
             f"({seconds:.0f}s around mid-point of {src.name})")
    tmpdir = tempfile.TemporaryDirectory(prefix="vcadapt_")
    try:
        root = Path(tmpdir.name)
        ref = root / "ref.mkv"
        extract_segment(tools.ffmpeg, src, center, min(seconds, duration), ref)

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
