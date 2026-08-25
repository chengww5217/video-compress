"""自适应 CRF 搜索：用 VMAF 反推「满足目标画质的最高 CRF」。

体积不是由某个 CRF 数字决定的，而是由画质闸门决定的：从整片均匀抽几段，在
样本上二分搜索，取最差段做判据。搜索结束时顺带把样本实测码率带出去，让调用
方在动手编码之前就能算出「这文件值不值得压几个小时」。
"""

import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import encode, proc, vmaf
from .logfmt import em
from .util import to_float, to_int


def _encode_sample(ffmpeg: str, ref: Path, crf: float, encoder_cfg: dict,
                   out: Path):
    """把一段参考片段按给定 CRF 编一遍，用来量分数和称体积。"""
    cmd = [
        ffmpeg, "-hide_banner", "-y",
        "-i", str(ref),
        *encode.video_args(encoder_cfg, crf),
        "-an",
        str(out),
    ]
    rc, _out, err = proc.run_managed(cmd)
    if rc != 0:
        raise encode.EncodeError(
            f"sample encode failed at crf={encode.fmt_crf(crf)}: "
            f"{err.strip()[-300:]}")


@dataclass(frozen=True)
class CrfChoice:
    """自适应搜索的结论。

    除了选中的 CRF，还带上样本实测的视频码率和是否真的达到了目标画质——这两个
    信息能在编码开始前就判断「这文件值不值得压几个小时」。
    """

    crf: float
    video_bitrate: int = 0
    meets_target: bool = True
    worst_vmaf: float = 0.0


def _probe_crf(tools, refs: list, crf, enc_cfg: dict, root: Path,
               target: float, model: str, log) -> tuple:
    """在所有样本段上试这个 CRF，返回 (最差段分数, 样本总字节, 量过的段数)。

    取最差而不是平均：平均分能被好段抬起来，而观感是由最差处决定的。某段已经
    不达标时后面的段就不用再量了，直接判这个 CRF 不行。
    """
    worst = None
    total_bytes = 0
    measured = 0
    tag = encode.fmt_crf(crf).replace(".", "_")
    for i, ref in enumerate(refs):
        sample = root / f"s{tag}_{i}.mkv"
        try:
            _encode_sample(tools.ffmpeg, ref, crf, enc_cfg, sample)
            size = sample.stat().st_size
            score = vmaf.measure(tools.ffmpeg, sample, ref, model=model, log=log)
        finally:
            sample.unlink(missing_ok=True)
        total_bytes += size
        measured += 1
        log.debug(f"[搜索] crf={encode.fmt_crf(crf)} 第 {i + 1}/{len(refs)} 段："
                  f"VMAF {score:.2f}，{size} 字节")
        worst = score if worst is None else min(worst, score)
        if worst < target:
            break
    return (worst if worst is not None else 0.0), total_bytes, measured


def search_midpoint(lo: float, hi: float, step: float) -> float:
    """二分的中点，对齐到步长网格且严格落在 (lo, hi) 内，保证循环收敛。"""
    mid = encode.quantize_crf(lo + (hi - lo) / 2)
    if step >= encode.CRF_GRID:
        mid = round(mid / step) * step
        mid = encode.quantize_crf(mid)
    return min(max(mid, encode.quantize_crf(lo + step)),
               encode.quantize_crf(hi - step))


def _sample_bitrate(total_bytes: int, span: float, segments: int) -> int:
    """样本总字节换算成码率。样本是均匀铺开抽的，可以代表全片平均。"""
    seconds = span * segments
    if seconds <= 0 or total_bytes <= 0:
        return 0
    return int(total_bytes * 8 / seconds)


def pick_crf(tools, src: Path, duration: float, enc_cfg: dict, ad_cfg: dict,
             log, lossless_codec: str = "libx264",
             model: str = "") -> "CrfChoice | None":
    target = to_float(ad_cfg.get("target_vmaf"), 95.0)
    lo = encode.quantize_crf(to_float(ad_cfg.get("crf_min"), 16))
    hi = encode.quantize_crf(to_float(ad_cfg.get("crf_max"), 50))
    step = max(encode.CRF_GRID, to_float(ad_cfg.get("search_step"),
                                         encode.CRF_GRID))
    seconds = to_float(ad_cfg.get("sample_seconds"), 10.0)
    count = max(1, to_int(ad_cfg.get("sample_count"), 4))

    starts = vmaf.sample_starts(duration, seconds, count)
    span = min(seconds, duration) if duration > 0 else seconds
    best = None
    log.info(f"[搜索] {em(src.name)}：抽 {len(starts)} 段 × {seconds:.0f}s "
             f"作为参考样本")
    tmpdir = tempfile.TemporaryDirectory(prefix="vcadapt_")
    try:
        root = Path(tmpdir.name)
        refs = []
        for i, start in enumerate(starts):
            ref = root / f"ref{i}.mkv"
            vmaf.extract_segment(tools.ffmpeg, src, start, span, ref,
                                 lossless_codec)
            refs.append(ref)

        while hi - lo > step:
            mid = search_midpoint(lo, hi, step)
            score, sample_bytes, measured = _probe_crf(
                tools, refs, mid, enc_cfg, root, target, model, log)
            log.info(f"[搜索] crf={em(encode.fmt_crf(mid))}：最差段 VMAF "
                     f"{em(f'{score:.2f}')}（目标 {target}）")
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
                    f"连 crf={encode.fmt_crf(lo)} 都只有 VMAF "
                    f"{score:.2f}，达不到目标 {target}"
                )
            best = CrfChoice(lo, _sample_bitrate(sample_bytes, span, measured),
                             score >= target, score)
        log.info(f"[搜索] 定为 crf={em(encode.fmt_crf(best.crf))}"
                 f"（目标 VMAF {target}，{len(refs)} 段里最差 "
                 f"{best.worst_vmaf:.2f}，样本码率 "
                 f"{best.video_bitrate // 1000} kbps）")
        return best
    except (encode.EncodeError, vmaf.VmafError, OSError) as e:
        log.warning(f"自适应 CRF 搜索失败（{e}），回退到配置里的固定 CRF")
        return None
    finally:
        tmpdir.cleanup()
