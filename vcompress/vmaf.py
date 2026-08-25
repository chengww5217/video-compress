"""画质测量：抽无损样本片段 + 用 libvmaf 打分。

抽样和打分必须放在一起：切点位置、抽样用的无损编码器、逐帧对齐方式，存在的
唯一目的就是让打出来的分数有意义。自适应搜索（试编码）和产物校验（抽检）
共用这一套器械，两边才是同一把尺子。
"""

import json
import tempfile
from pathlib import Path

from . import proc
from .util import to_int


class VmafError(RuntimeError):
    """量一段画质失败——抽样和打分都算，调用方一律当成"这次量不出来"。"""


# ---------------------------------------------------------------------- 抽样

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


def sample_starts(duration: float, seconds: float, count: int,
                  fps: float = 0.0) -> list:
    """把 count 段样本均匀铺在整片上，返回每段的起点。

    只取中间一段是不够的：一部片子的中点可能是明亮的动作戏，也可能是全黑的
    过场，按它定的 CRF 拿到暗场或高动态段就不成立了。均匀铺开并按最差段决策，
    才和后面用多段抽检的校验是同一个口径。

    自适应搜索和 VMAF 抽检共用这一个函数：两处各写一份的时候，一份有短片去重、
    另一份有帧对齐，铺开的位置却要求是同一套口径，迟早对不上。给了 fps 就把
    切点挪到帧间隙（见 frame_aligned_start），抽检那边必须这么做。
    """
    count = max(1, count)
    if duration <= 0:
        return [0.0]
    span = min(seconds, duration)
    starts = []
    for i in range(count):
        frac = (i + 0.5) / count
        start = max(0.0, min(duration - span, duration * frac - span / 2))
        starts.append(frame_aligned_start(start, fps) if fps > 0 else start)
    # 片子太短时几段会挤在一起，去重免得白量/白编同一段
    unique = []
    for start in starts:
        if all(abs(start - kept) > 0.25 for kept in unique):
            unique.append(start)
    return unique


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
        raise VmafError(f"抽取样本片段失败：{err.strip()[-300:]}")


# ------------------------------------------------------------------ 模型与打分

# Netflix 只发布了 1080p 和 4K 两套模型。默认模型按「1080p 屏幕、3 倍屏高
# 观看距离」训练，用在 4K 片源上是错的量具；4K 模型对应「4K 屏幕、1.5 倍屏高」。
# 同样的失真在 4K 模型下分数更高（实测同一对文件 95.0 -> 97.4），因为那个观看
# 条件下本来就更不容易看出来。1440p 这类中间分辨率仍用默认模型：它离 1080p 的
# 观看条件更近。
MODEL_4K = "vmaf_4k_v0.6.1"


def model_for(width: int, height: int, enabled: bool = True) -> str:
    """按分辨率挑 VMAF 模型；返回空串表示用 libvmaf 的默认模型。"""
    if not enabled:
        return ""
    if to_int(width) >= 3000 or to_int(height) >= 1700:
        return MODEL_4K
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
            raise VmafError(
                f"libvmaf {key} score is not a number: {entry[key]!r}") from e
    raise VmafError("libvmaf 的输出里没有汇总分数")


def lavfi(shift: int = 0, model: str = "") -> str:
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


def measure(ffmpeg: str, distorted: Path, reference: Path, shift: int = 0,
            model: str = "", pool: str = "harmonic_mean", log=None):
    """量一对片段的 VMAF；指定的模型加载不了时退回默认模型。

    退回这一步是必要的：模型名不被认识时 libvmaf 直接报错退出，而校验链把
    「libvmaf 用不了」当成告警放过——4K 片源会因此全部跳过画质门槛。
    """
    try:
        return _measure(ffmpeg, distorted, reference, shift, model, pool)
    except VmafError:
        if not model:
            raise
        if log:
            log.warning(f"这个构建里没有 libvmaf 模型 {model}，"
                        f"回退到默认模型")
        return _measure(ffmpeg, distorted, reference, shift, "", pool)


def _measure(ffmpeg: str, distorted: Path, reference: Path, shift: int,
             model: str, pool: str):
    with tempfile.TemporaryDirectory(prefix="vcvmaf_") as td:
        log_path = Path(td) / "vmaf.json"
        cmd = [
            ffmpeg, "-hide_banner",
            "-i", str(distorted),
            "-i", str(reference),
            "-lavfi", lavfi(shift, model),
            "-f", "null", "-",
        ]
        rc, _out, err = proc.run_managed(cmd, cwd=td)
        if rc != 0:
            raise VmafError(f"libvmaf 执行失败：{err.strip()[-300:]}")
        try:
            data = json.loads(log_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise VmafError(f"读不了 libvmaf 的输出：{e}") from e
        return _pooled_score(data, pool)
