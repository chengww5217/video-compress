import json
import shlex
import subprocess
import tempfile
import threading
from pathlib import Path

_active_procs = set()
_lock = threading.Lock()


class EncodeError(RuntimeError):
    pass


def register(proc):
    with _lock:
        _active_procs.add(proc)


def unregister(proc):
    with _lock:
        _active_procs.discard(proc)


def terminate_all():
    with _lock:
        procs = list(_active_procs)
    for p in procs:
        try:
            p.kill()
        except OSError:
            pass


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
              crf: int, a_args: list, marker: str) -> list:
    return [
        ffmpeg, "-hide_banner", "-y",
        "-i", str(src),
        "-map", "0:V", "-map", "0:a?", "-map", "0:s?",
        "-map", "0:t?", "-map", "0:d?",
        "-map_metadata", "0",
        "-map_chapters", "0",
        *video_args(encoder_cfg, crf),
        *a_args,
        "-c:s", "copy", "-c:t", "copy", "-c:d", "copy",
        "-metadata", f"comment={marker}",
        "-movflags", "+use_metadata_tags",
        "-progress", "pipe:1", "-nostats",
        str(dst),
    ]


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
    err_tail = ""
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line or not progress_cb or total_duration <= 0:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            t_us = data.get("out_time_us") or data.get("out_time_ms")
            if t_us:
                pct = min(99.9, float(t_us) / 1e6 / total_duration * 100)
                progress_cb(pct)
        err_tail = proc.stderr.read()[-2000:]
        rc = proc.wait()
    except BaseException:
        try:
            proc.kill()
        except OSError:
            pass
        raise
    finally:
        unregister(proc)
    if rc != 0:
        raise EncodeError(f"ffmpeg exited {rc}: {err_tail.strip()[-500:]}")


def _run_quiet(cmd: list) -> tuple:
    r = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return r.returncode, r.stderr


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
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=td,
        )
        if r.returncode != 0:
            raise EncodeError(f"libvmaf failed: {r.stderr.strip()[-300:]}")
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
            log.debug(f"adaptive probe crf={mid} vmaf={score:.2f}")
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
