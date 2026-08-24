import collections
import json
import shlex
import subprocess
import tempfile
import threading
import time
from pathlib import Path

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


def audio_args(info, cfg) -> list:
    threshold = int(cfg["audio"].get("transcode_over_kbps", 512)) * 1000
    lossless = ("pcm_", "flac", "alac", "ape", "wavpack", "truehd")
    needs_transcode = False
    for codec, br in zip(info.audio_codecs, info.audio_bitrates):
        if codec.startswith(lossless):
            needs_transcode = True
        elif br and br > threshold:
            needs_transcode = True
    if not info.audio_codecs:
        return []
    if needs_transcode:
        kbps = int(cfg["audio"].get("opus_kbps", 160))
        return ["-c:a", "libopus", "-b:a", f"{kbps}k"]
    return ["-c:a", "copy"]


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


def extract_reference_segment(ffmpeg: str, src: Path, start: float,
                              seconds: float, out: Path):
    cmd = [
        ffmpeg, "-hide_banner", "-y",
        "-i", str(src),
        "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}",
        "-map", "0:v:0", "-an", "-sn", "-dn",
        "-c:v", "libx264", "-crf", "0", "-preset", "ultrafast",
        str(out),
    ]
    rc, err = _run_quiet(cmd)
    if rc != 0:
        raise EncodeError(f"reference segment failed: {err.strip()[-300:]}")


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


def vmaf_score(ffmpeg: str, distorted: Path, reference: Path):
    with tempfile.TemporaryDirectory(prefix="vcvmaf_") as td:
        log_path = Path(td) / "vmaf.json"
        cmd = [
            ffmpeg, "-hide_banner",
            "-i", str(distorted),
            "-i", str(reference),
            "-lavfi", "libvmaf=log_path=vmaf.json:log_fmt=json",
            "-f", "null", "-",
        ]
        rc, _out, err = run_managed(cmd, cwd=td)
        if rc != 0:
            raise EncodeError(f"libvmaf failed: {err.strip()[-300:]}")
        data = json.loads(log_path.read_text(encoding="utf-8"))
        pooled = data.get("pooled_metrics", {})
        vmaf = pooled.get("vmaf") or next(iter(pooled.values()), {})
        return float(vmaf.get("mean"))


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
        extract_reference_segment(tools.ffmpeg, src, center, min(seconds, duration), ref)

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
