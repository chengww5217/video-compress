import subprocess
import tempfile
from pathlib import Path

from . import encode


def decode_errors(ffmpeg: str, path: Path):
    r = subprocess.run(
        [ffmpeg, "-hide_banner", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    errs = [ln for ln in r.stderr.splitlines() if ln.strip()]
    if r.returncode != 0 or errs:
        return errs[:10]
    return None


def check_basic(orig_info, out_info, orig_size: int, new_size: int, cfg):
    max_diff = float(cfg["verify"].get("max_duration_diff_s", 0.5))
    if orig_info.duration > 0 and out_info.duration > 0:
        diff = abs(orig_info.duration - out_info.duration)
        if diff > max_diff:
            return False, f"duration mismatch {diff:.2f}s > {max_diff}s"
    if not out_info.video_codec:
        return False, "output has no video stream"
    if new_size >= orig_size:
        return False, f"output {new_size} bytes >= source {orig_size} bytes"
    min_saving = float(cfg["verify"].get("min_actual_saving", 0.05))
    saving = 1.0 - new_size / orig_size
    if saving < min_saving:
        return False, f"saving only {saving:.1%} < {min_saving:.0%}, discarding"
    return True, ""


def _normalized_segment(ffmpeg: str, src: Path, start: float, seconds: float,
                        out: Path):
    cmd = [
        ffmpeg, "-hide_banner", "-y",
        "-i", str(src),
        "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}",
        "-map", "0:v:0", "-an", "-sn", "-dn",
        "-c:v", "libx264", "-crf", "0", "-preset", "ultrafast",
        str(out),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise encode.EncodeError(f"segment extract failed: {r.stderr.strip()[-300:]}")


def vmaf_spot_check(tools, orig: Path, compressed: Path, duration: float, cfg):
    if not cfg["verify"].get("vmaf_enabled", True):
        return None, "disabled"
    segments = int(cfg["verify"].get("vmaf_segments", 3))
    seconds = float(cfg["verify"].get("vmaf_sample_seconds", 20))
    threshold = float(cfg["verify"].get("vmaf_threshold", 93.0))

    scores = []
    with tempfile.TemporaryDirectory(prefix="vcvmaf_") as td:
        root = Path(td)
        for i in range(segments):
            frac = (i + 0.5) / segments
            start = max(0.0, min(duration - seconds, duration * frac - seconds / 2))
            ref = root / f"ref{i}.mkv"
            dist = root / f"dist{i}.mkv"
            try:
                _normalized_segment(tools.ffmpeg, orig, start, seconds, ref)
                _normalized_segment(tools.ffmpeg, compressed, start, seconds, dist)
                score = encode.vmaf_score(tools.ffmpeg, dist, ref)
                scores.append(score)
            except encode.EncodeError as e:
                return None, str(e)
    mean = sum(scores) / len(scores)
    if mean < threshold:
        return mean, (
            f"vmaf mean {mean:.2f} below threshold {threshold} "
            f"(segments: {', '.join(f'{s:.2f}' for s in scores)})"
        )
    return mean, ""
