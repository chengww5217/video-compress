"""产物校验：解码无错、基础属性一致、色彩元数据一致、VMAF 抽检。

对外只需要 run_all()：四步校验的顺序、哪一步失败算硬失败、哪一步不可用只
告警，都是校验策略的一部分，不该散落在调用方里。
"""

import tempfile
from pathlib import Path

from . import encode, proc
from .util import to_float, to_int


class VerifyError(RuntimeError):
    """产物没通过校验；调用方据此丢弃产物并保留原文件。"""


def decode_errors(ffmpeg: str, path: Path):
    rc, _out, err = proc.run_managed(
        [ffmpeg, "-hide_banner", "-v", "error", "-i", str(path), "-f", "null", "-"]
    )
    errs = [ln for ln in err.splitlines() if ln.strip()]
    if errs:
        return errs[:10]
    if rc != 0:
        return [f"decode exited {rc} without printing a diagnostic"]
    return None


def check_basic(orig_info, out_info, orig_size: int, new_size: int, cfg):
    max_diff = to_float(cfg["verify"].get("max_duration_diff_s"), 0.5)
    if orig_info.duration > 0 and out_info.duration > 0:
        diff = abs(orig_info.duration - out_info.duration)
        if diff > max_diff:
            return False, f"duration mismatch {diff:.2f}s > {max_diff}s"
    if not out_info.video_codec:
        return False, "output has no video stream"
    # 分辨率必须一致：滤镜链写错、编码器悄悄降采样都会在这里暴露
    if orig_info.width and orig_info.height and out_info.width and out_info.height:
        if (orig_info.width, orig_info.height) != (out_info.width, out_info.height):
            return False, (
                f"resolution changed {orig_info.width}x{orig_info.height} -> "
                f"{out_info.width}x{out_info.height}"
            )
    for kind, before, after in (
        ("audio", len(orig_info.audio_codecs), len(out_info.audio_codecs)),
        ("subtitle", len(orig_info.subtitle_codecs),
         len(out_info.subtitle_codecs)),
    ):
        if before != after:
            return False, f"{kind} stream count changed {before} -> {after}"
    if new_size >= orig_size:
        return False, f"output {new_size} bytes >= source {orig_size} bytes"
    min_saving = to_float(cfg["verify"].get("min_actual_saving"), 0.05)
    saving = 1.0 - new_size / orig_size
    if saving < min_saving:
        return False, f"saving only {saving:.1%} < {min_saving:.0%}, discarding"
    return True, ""


COLOR_FIELDS = (
    ("color_primaries", "primaries"),
    ("color_transfer", "transfer"),
    ("color_space", "matrix"),
    ("color_range", "range"),
)


def check_color(orig_info, out_info, cfg):
    """比对色彩属性与 HDR 母版信息是否原样带过来了。

    这一项不能交给 VMAF：色彩属性和 HDR 母版信息丢失时，解码出来的像素值可以
    完全不变（VMAF 照样 99 分），但播放器不知道这是 BT.2020/PQ，会按 SDR 显示，
    画面发灰。逐帧比对压根测不到这种「视觉有损」。

    只在源确实标了某项时才要求产物一致：源本来就没标（unknown）而编码器补了个
    默认值，不算退化。
    """
    if not cfg["verify"].get("color_check", True):
        return True, ""
    problems = []
    for attr, label in COLOR_FIELDS:
        before = getattr(orig_info, attr, "") or ""
        after = getattr(out_info, attr, "") or ""
        if not before:
            continue
        if after != before:
            problems.append(f"{label} {before} -> {after or 'unset'}")
    before_hdr = set(getattr(orig_info, "hdr_metadata", ()) or ())
    after_hdr = set(getattr(out_info, "hdr_metadata", ()) or ())
    lost = sorted(before_hdr - after_hdr)
    if lost:
        problems.append("lost " + ", ".join(lost))
    if problems:
        return False, "color metadata changed: " + "; ".join(problems)
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
                    cfg, log=None, fps: float = 0.0,
                    lossless_codec: str = "libx264"):
    vcfg = cfg["verify"]
    if not vcfg.get("vmaf_enabled", True):
        return None, ""
    segments = max(1, to_int(vcfg.get("vmaf_segments"), 3))
    seconds = to_float(vcfg.get("vmaf_sample_seconds"), 20.0)
    threshold = to_float(vcfg.get("vmaf_threshold"), 93.0)
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
                encode.extract_segment(tools.ffmpeg, orig, start, seconds, ref,
                                       lossless_codec)
                encode.extract_segment(tools.ffmpeg, compressed, start,
                                       seconds, dist, lossless_codec)
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


def run_all(tools, src: Path, out: Path, orig_info, out_info, cfg,
            log=None, lossless_codec: str = "libx264"):
    """跑完整校验链，任何硬失败抛 VerifyError；返回 VMAF 均值（可能为 None）。

    顺序是刻意的：先做便宜的（解码、时长、体积、色彩元数据），最后才做最贵的
    VMAF 抽检。
    """
    if cfg["verify"].get("decode_check", True):
        errs = decode_errors(tools.ffmpeg, out)
        if errs:
            raise VerifyError("decode check failed: " + "; ".join(errs[:3]))

    ok, msg = check_basic(orig_info, out_info, orig_info.size,
                          out.stat().st_size, cfg)
    if not ok:
        raise VerifyError(msg)

    ok, msg = check_color(orig_info, out_info, cfg)
    if not ok:
        raise VerifyError(msg)

    score, msg = vmaf_spot_check(tools, src, out, orig_info.duration, cfg,
                                 log=log, fps=orig_info.fps,
                                 lossless_codec=lossless_codec)
    if msg and score is None:
        # 构建里没有 libvmaf 之类的环境问题：告警但不否决，其余检查已经过了
        if log:
            log.warning(f"{src.name}: vmaf check unavailable ({msg}); proceeding")
    elif msg:
        raise VerifyError(msg)
    return score
