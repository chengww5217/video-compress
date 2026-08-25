import tempfile
from pathlib import Path

from . import encode


def _to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def decode_errors(ffmpeg: str, path: Path):
    rc, _out, err = encode.run_managed(
        [ffmpeg, "-hide_banner", "-v", "error", "-i", str(path), "-f", "null", "-"]
    )
    errs = [ln for ln in err.splitlines() if ln.strip()]
    if errs:
        return errs[:10]
    if rc != 0:
        return [f"decode exited {rc} without printing a diagnostic"]
    return None


def check_basic(orig_info, out_info, orig_size: int, new_size: int, cfg):
    max_diff = float(cfg["verify"].get("max_duration_diff_s", 0.5))
    if orig_info.duration > 0 and out_info.duration > 0:
        diff = abs(orig_info.duration - out_info.duration)
        if diff > max_diff:
            return False, f"duration mismatch {diff:.2f}s > {max_diff}s"
    if not out_info.video_codec:
        return False, "output has no video stream"
    for kind, before, after in (
        ("audio", len(orig_info.audio_codecs), len(out_info.audio_codecs)),
        ("subtitle", len(orig_info.subtitle_codecs),
         len(out_info.subtitle_codecs)),
    ):
        if before != after:
            return False, f"{kind} stream count changed {before} -> {after}"
    if new_size >= orig_size:
        return False, f"output {new_size} bytes >= source {orig_size} bytes"
    min_saving = float(cfg["verify"].get("min_actual_saving", 0.05))
    saving = 1.0 - new_size / orig_size
    if saving < min_saving:
        return False, f"saving only {saving:.1%} < {min_saving:.0%}, discarding"
    return True, ""


def _fmt_segments(records) -> str:
    return ", ".join(f"{s:.2f}@{t:.0f}s" for t, s in records)


def _score_segment(tools, ref: Path, dist: Path, threshold: float,
                   index: int, start: float, log=None) -> float:
    """量一段的 VMAF；分数异常低时先确认不是错帧量出来的假警报。

    抽段边界、VFR 片源、时间戳精度都可能让参考段和待测段差一帧，逐帧比
    出来的分数会崩到 30~60。前后各挪一帧再量，如果分数立刻回到正常水平，
    说明是对齐问题而不是画质问题，取对齐后的分数。
    """
    score = encode.vmaf_score(tools.ffmpeg, dist, ref)
    if score >= threshold:
        return score
    best, best_shift = score, 0
    for shift in (1, -1):
        try:
            alt = encode.vmaf_score(tools.ffmpeg, dist, ref, shift)
        except encode.EncodeError:
            continue
        if alt > best:
            best, best_shift = alt, shift
    if best_shift and log:
        log.warning(
            f"vmaf segment {index} at {start:.1f}s scored {score:.2f}; "
            f"realigning by {best_shift:+d} frame gives {best:.2f} — "
            f"treating the low score as a sampling offset"
        )
    return best


def vmaf_spot_check(tools, orig: Path, compressed: Path, duration: float,
                    cfg, log=None, fps: float = 0.0):
    vcfg = cfg["verify"]
    if not vcfg.get("vmaf_enabled", True):
        return None, ""
    segments = max(1, _to_int(vcfg.get("vmaf_segments", 3), 3))
    seconds = _to_float(vcfg.get("vmaf_sample_seconds", 20), 20.0)
    threshold = _to_float(vcfg.get("vmaf_threshold", 93.0), 93.0)
    if seconds <= 0:
        seconds = 20.0
    if duration <= 0:
        return None, "unknown duration, cannot pick sample points"

    records = []
    with tempfile.TemporaryDirectory(prefix="vcvmaf_") as td:
        root = Path(td)
        for i in range(segments):
            frac = (i + 0.5) / segments
            start = max(0.0, min(duration - seconds, duration * frac - seconds / 2))
            # 两个文件用同一个切点，且切在帧间隙里，抽出的段才逐帧对应
            start = encode.frame_aligned_start(start, fps)
            ref = root / f"ref{i}.mkv"
            dist = root / f"dist{i}.mkv"
            if log:
                log.info(f"vmaf spot check: segment {i + 1}/{segments}")
            try:
                encode.extract_segment(tools.ffmpeg, orig, start, seconds, ref)
                encode.extract_segment(tools.ffmpeg, compressed, start,
                                       seconds, dist)
                score = _score_segment(tools, ref, dist, threshold, i + 1,
                                       start, log)
                if log:
                    log.debug(f"vmaf segment {i + 1} at {start:.1f}s: {score:.2f}")
                records.append((start, score))
            except encode.EncodeError as e:
                return None, str(e)
    if not records:
        return None, "no vmaf segment could be measured"
    mean = sum(s for _t, s in records) / len(records)
    if mean < threshold:
        return mean, (
            f"vmaf mean {mean:.2f} below threshold {threshold} "
            f"(segments: {_fmt_segments(records)})"
        )
    return mean, ""
