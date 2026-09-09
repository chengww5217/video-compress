"""重封装模式：把「不用再压」的文件修成快进友好的形态。

快进卡顿的两个来源都在这里处理：

1. **GOP 太长**。播放器 seek 时必须先退到上一个关键帧、再逐帧解码到目标
   位置——间隔 10 秒就意味着最坏要解几百帧，拖动进度条就是一次可感知的
   卡顿（本工具自己的压缩输出用的就是 keyint=257 ≈ 10 秒）。修复它只能
   重编码视频轨：关键帧的位置在码流里，流复制插不进去。
2. **老容器没有可靠的索引**。avi/flv/wmv/mpg 系只能线性扫描找 seek 点。
   修复它用流复制就够：画面逐字节不变，只换容器。

这个模块只做决策和参数生成；文件落地、失败清理等编排见 pipeline.py。

和压缩模式的三个口径差异（都由 [remux] 配置）：

- 画质闸门更严：重编码是二次编码（代际损失叠加），目标 VMAF 从 95 提到
  98，达不到就不动——「视觉无损优先」的前提下，为了修快进把画质压坏是
  本末倒置；
- 体积闸门反向：压缩要求至少省 15%，重封装允许最多**涨** 10%——关键帧
  不免费，多出来的关键帧就是体积代价；
- 音轨一律复制：重封装的承诺是「只动该动的」，音轨重编码省的那点体积
  不在承诺范围内。
"""

from dataclasses import dataclass
from pathlib import Path

from . import encode, proc, verify
from .logfmt import em
from .util import fmt_size, to_float

# 没有可靠 seek 索引的容器：快进要靠线性扫描，值得换容器
LEGACY_CONTAINERS = {".avi", ".flv", ".wmv", ".mpg", ".mpeg"}

# mp4 系容器装不下的编码器：流复制到 mp4 会直接失败。mkv 系什么都能装。
MP4_SAFE_CODECS = {"h264", "hevc", "av1", "vp9", "mpeg4", "mpeg2video"}

# keyint 上限：SVT-AV1 的 keyint 参数有合法区间，绑定时宁可往下取——
# keyint 变小只会让关键帧更密（间隔承诺更有保障），变大才会失约。
MAX_KEYINT = 512

GOP = "gop"    # 重编码视频轨，修关键帧间隔
COPY = "copy"  # 流复制换容器，修索引


@dataclass(frozen=True)
class RemuxPlan:
    """重封装决策：strategy 为空串表示没什么可修的。"""

    strategy: str
    reason: str
    max_gap: float
    keyint: int = 0


# ------------------------------------------------------------------ 关键帧间隔

def keyframe_timestamps(tools, path: Path) -> list:
    """扫全片包列表，返回关键帧时间戳（秒，升序）。

    走 ffprobe 的包枚举而不是解码：不碰像素，代价是通读一遍文件的容器层
    （大文件几十秒，相对几小时的重编码可以忽略）。不能用抽样窗口代替：
    关键帧最长的缺口可能在任何两扇窗口之间，漏看就等于没修。

    pts 和 dts 都要：AVI 这类容器只有 dts，只看 pts 会把所有关键帧都读丢。
    """
    cmd = [
        tools.ffprobe,
        "-v", "error",
        "-select_streams", "V:0",
        "-show_entries", "packet=pts_time,dts_time,flags",
        "-of", "csv=p=0",
        str(path),
    ]
    rc, out, err = proc.run_managed(cmd)
    if rc != 0:
        raise RuntimeError(
            f"扫关键帧失败：{proc.error_tail(err)}"
        )
    stamps = []
    for line in out.splitlines():
        fields = [f.strip() for f in line.split(",") if f.strip()]
        if len(fields) < 3 or "K" not in fields[-1]:
            continue
        # 前两列是 pts/dts，取第一个能解析的；都是 "N/A" 就跳过这个包
        for value in fields[:-1]:
            try:
                stamps.append(float(value))
                break
            except ValueError:
                continue
    return stamps


def max_keyframe_gap(stamps: list, duration: float) -> float:
    """相邻关键帧的最大间隔，含片头片尾两段。

    只有一个关键帧（常见于「首帧之后再也不给关键帧」的劣质编码）时，
    整片就是一个 GOP，间隔就是时长。
    """
    if not stamps:
        return 0.0
    points = sorted(stamps)
    end = duration if duration > 0 else points[-1]
    gaps = [points[0]]  # 片头：从 0 到第一个关键帧
    gaps += [b - a for a, b in zip(points, points[1:])]
    gaps += [max(0.0, end - points[-1])]  # 片尾：最后一个关键帧到结束
    return max(gaps)


# ---------------------------------------------------------------------- 决策

def keyint_frames(fps: float, gap_s: float) -> int:
    """按帧率和间隔上限算 keyint（帧数），并夹到编码器吃得下的范围。"""
    rate = fps if fps and fps > 0 else 25.0
    return max(1, min(MAX_KEYINT, round(rate * max(0.1, gap_s))))


def copy_fits_container(container: str, codec: str) -> bool:
    """源编码器能不能流复制进目标容器。mkv 系什么都能装。"""
    from .tracks import MKV_LIKE
    if (container or "").lower() in MKV_LIKE:
        return True
    return (codec or "").lower() in MP4_SAFE_CODECS


def plan_remux(info, cfg: dict, max_gap: float) -> RemuxPlan:
    """决策树：GOP 超限 -> 重编码；容器老旧 -> 流复制；都没问题 -> 不动。"""
    rcfg = cfg["remux"]
    gap_s = to_float(rcfg.get("max_keyframe_gap_s"), 5.0)
    if max_gap > gap_s:
        # 交错源的 GOP 修复必须重编码，而重编码丢交错标记（AV1 没有交错
        # 信号），这是 VMAF 测不出来的视觉有损——和压缩模式同一个理由，
        # 只能放行到流复制那一步
        if info.interlaced:
            return RemuxPlan("", (
                f"关键帧间隔 {max_gap:.0f}s 超过 {gap_s:.0f}s，但交错源"
                f"重编码会丢交错标记，不敢动"
            ), max_gap)
        return RemuxPlan(GOP, (
            f"关键帧最长间隔 {max_gap:.0f}s，超过 {gap_s:.0f}s"
        ), max_gap, keyint_frames(info.fps, gap_s))
    if info.container in LEGACY_CONTAINERS:
        target = (cfg["encode"].get("output_container") or "").lower()
        if info.container == target:
            return RemuxPlan("", (
                f"容器 {info.container} 没有可靠索引，但目标容器也是它，"
                f"换了也一样"
            ), max_gap)
        if not copy_fits_container(target, info.video_codec):
            return RemuxPlan("", (
                f"容器 {info.container} 没有可靠索引，但源编码器 "
                f"{info.video_codec} 装不进 {target}"
            ), max_gap)
        return RemuxPlan(COPY, (
            f"容器 {info.container} 没有可靠的 seek 索引"
        ), max_gap)
    return RemuxPlan("", "关键帧间隔与容器都没有问题", max_gap)


# ------------------------------------------------------------------ 编码参数

def bound_keyint_cfg(enc_cfg: dict, fps: float, gap_s: float) -> dict:
    """把 keyint 绑定到间隔上限，塞进编码器自己的参数串。

    只认得出参数串写法的两个编码器；别的编码器不乱塞参数，原样返回
    （它们的默认 GOP 一般也短不到哪去，修不到就修不到，不能瞎改）。
    """
    frames = keyint_frames(fps, gap_s)
    cfg = dict(enc_cfg)
    codec = (cfg.get("codec") or "").lower()
    if codec == "libsvtav1":
        cfg["extra"] = encode.merge_dict_param(cfg.get("extra", ""),
                                               "-svtav1-params",
                                               "keyint", str(frames))
    elif codec == "libx265":
        cfg["extra"] = encode.merge_dict_param(cfg.get("extra", ""),
                                               "-x265-params",
                                               "keyint", str(frames))
    else:
        return enc_cfg
    return cfg


def adaptive_cfg(cfg: dict) -> dict:
    """重封装用的自适应配置：[adaptive] 整体沿用，只换画质目标。

    抽样段数、搜索步长这些必须和压缩模式同一套——两边共用 vmaf 的取样
    代码，改了就对不上口径了。
    """
    ad = dict(cfg["adaptive"])
    ad["target_vmaf"] = to_float(cfg["remux"].get("target_vmaf"), 98.0)
    return ad


def predicted_size(info, video_bitrate: int):
    """按样本实测码率预估重编码产物体积；信息不全时返回 None。

    音轨按复制算（重封装不重编音频），所以和 tracks.predicted_size 不是
    一回事——那边用的是会转码的音轨计划。有音轨码率未知时照样估不准，
    宁可不猜，交给编码后的实测闸门。
    """
    if video_bitrate <= 0 or info.duration <= 0:
        return None
    if any(br <= 0 for br in info.audio_bitrates):
        return None  # 有音轨码率不明，整个预估就不可信，宁可不猜
    audio = sum(br for br in info.audio_bitrates if br)
    return int((video_bitrate + audio) * info.duration / 8)


def growth_limit(size: int, cfg: dict) -> int:
    """允许的体积上限：源大小 ×（1 + max_growth_ratio）。"""
    ratio = to_float(cfg["remux"].get("max_growth_ratio"), 0.10)
    return int(size * (1.0 + max(0.0, ratio)))


# ---------------------------------------------------------------------- 校验

def verify_copy(tools, src: Path, out: Path, orig_info, out_info,
                cfg: dict, log=None):
    """流复制产物的校验：基础一致性、色彩/HDR、解码、体积。

    不量 VMAF：视频流的字节逐个照搬，量出来的分数必然接近满分，白花
    几十分钟抽检。解码校验保留——封装环节的 bug（装错轨、时间戳坏了）
    只有它抓得住。
    """
    ok, msg = verify.check_basic(orig_info, out_info, cfg)
    if not ok:
        raise verify.VerifyError(msg)
    ok, msg = verify.check_color(orig_info, out_info, cfg)
    if not ok:
        raise verify.VerifyError(msg)
    if cfg["verify"].get("decode_check", True):
        errs = verify.added_decode_errors(tools.ffmpeg, out, src, log=log)
        if errs:
            raise verify.VerifyError("解码校验没过：" + "；".join(errs[:3]))
    new_size = out.stat().st_size
    limit = growth_limit(orig_info.size, cfg)
    if new_size > limit:
        # 和 check_size 同类的「重跑也一样」结论：换容器多出来的开销是
        # 确定的，不是环境问题
        raise verify.PermanentVerifyError(
            f"重封装后反而大了：{fmt_size(orig_info.size)} -> "
            f"{fmt_size(new_size)}，超过允许的上限 {fmt_size(limit)}"
        )


def fmt_gap(seconds: float) -> str:
    """关键帧间隔的人话写法：够一分钟按分钟计，否则按秒。"""
    if seconds >= 60:
        return f"{seconds / 60:.0f} 分钟"
    return f"{seconds:.0f}s"


def describe(plan: RemuxPlan, gap_s: float) -> str:
    """预览/日志里对决策的一句话说明。"""
    if plan.strategy == GOP:
        return (f"重编码修关键帧间隔（{fmt_gap(plan.max_gap)} -> "
                f"最多 {fmt_gap(gap_s)}，keyint={plan.keyint}）")
    if plan.strategy == COPY:
        return f"流复制换容器修 seek 索引（{plan.reason}）"
    return em(plan.reason)
