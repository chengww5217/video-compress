"""重封装模式：把「不用再压」的文件重编成快进友好的形态。

不做体检：「不用再压」的文件一律按配置的编码器重编视频轨，keyint 绑定到
[remux] max_keyframe_gap_s。以前先扫关键帧、间隔没超上限就放过，但在
播放机上的实测推翻了这个判据：关键帧间隔 4.17s 的 h264 原片快进明显卡，
重编成 5s 间隔的 AV1 反而顺——测出来的间隔和体感对不上，检测就没有意义。

这个模块只做参数生成和产物关键帧的复核；文件落地、失败清理等编排见
pipeline.py。

和压缩模式的三个口径差异（都由 [remux] 配置）：

- 画质闸门更严：重编码是二次编码（代际损失叠加），目标 VMAF 从 95 提到
  98，达不到就不动——「视觉无损优先」的前提下，为了修快进把画质压坏是
  本末倒置；
- 体积闸门反向：压缩要求至少省 15%，重封装允许最多**涨** 10%——关键帧
  不免费，多出来的关键帧就是体积代价；
- 音轨一律复制：重封装的承诺是「只动该动的」，音轨重编码省的那点体积
  不在承诺范围内。
"""

from pathlib import Path

from . import encode, proc
from .util import to_float

# keyint 上限：SVT-AV1 的 keyint 参数有合法区间，绑定时宁可往下取——
# keyint 变小只会让关键帧更密（间隔承诺更有保障），变大才会失约。
MAX_KEYINT = 512

# results.jsonl 里重封装记录的 strategy 字段，和老记录保持同一个值
GOP = "gop"


# ------------------------------------------------------------------ 关键帧间隔

def keyframe_timestamps(tools, path: Path) -> list:
    """扫全片包列表，返回关键帧时间戳（秒，升序）。重编完复核产物用。

    走 ffprobe 的包枚举而不是解码：不碰像素，代价是通读一遍文件的容器层
    （大文件几十秒，相对几小时的重编码可以忽略）。不能用抽样窗口代替：
    关键帧最长的缺口可能在任何两扇窗口之间，漏看就等于没验。

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


# ------------------------------------------------------------------ 编码参数

def keyint_frames(fps: float, gap_s: float) -> int:
    """按帧率和间隔上限算 keyint（帧数），并夹到编码器吃得下的范围。"""
    rate = fps if fps and fps > 0 else 25.0
    return max(1, min(MAX_KEYINT, round(rate * max(0.1, gap_s))))


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
