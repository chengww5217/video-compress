"""产物校验：解码无错、基础属性一致、色彩元数据一致、VMAF 抽检。

对外只需要 run_all()：四步校验的顺序、哪一步失败算硬失败、哪一步不可用只
告警，都是校验策略的一部分，不该散落在调用方里。

解码校验以源文件为基线：片源自带的解码错误不算产物的问题，详见
added_decode_errors()。
"""

import re
from collections import Counter
from pathlib import Path

from . import proc, scratch, vmaf
from .logfmt import em
from .util import to_float, to_int

# 收集上限：一个真损坏的文件能刷出成千上万行，留够比对用的量就行
MAX_DECODE_ERRORS = 500

# 每次运行都不同、或者两个文件之间本就会变的部分，比对前必须抹掉
_ADDR_RE = re.compile(r"@ 0x[0-9a-fA-F]+")
_STREAM_RE = re.compile(r"#\d+:\d+")
_REPEAT_RE = re.compile(r"Last message repeated (\d+) times?")


class VerifyError(RuntimeError):
    """产物没通过校验；调用方据此丢弃产物并保留原文件。"""


def decode_errors(ffmpeg: str, path: Path):
    rc, _out, err = proc.run_managed(
        [ffmpeg, "-hide_banner", "-v", "error", "-i", str(path), "-f", "null", "-"]
    )
    errs = [ln for ln in err.splitlines() if ln.strip()]
    if errs:
        return errs[:MAX_DECODE_ERRORS]
    if rc != 0:
        return [f"解码进程退出码 {rc}，但没打印任何诊断信息"]
    return None


def count_decode_errors(lines) -> Counter:
    """把解码错误行归一化成 {消息: 次数}，让两次解码的同一条消息能对上。

    要抹掉三样东西：

    - 对象地址（`[mp3float @ 0x5f34...]`）每次运行都不一样；
    - 流序号（`[aist#0:1/mp3]`）——同一条音轨在源和产物里的序号未必相同，
      AVI 里是 0:1、产物里可能是 0:0，不抹掉就会把继承来的错误当成新错误。
      消息里仍带着编码器名（`/mp3`、`dec:mp3float`），不会把视频的错误和
      音频的错误混为一谈；
    - "Last message repeated N times"：ffmpeg 把连续重复的消息折叠成这一行，
      不展开的话同一个缺陷在源和产物里的次数对不上。
    """
    counts = Counter()
    previous = ""
    for raw in lines or ():
        line = raw.strip()
        if not line:
            continue
        m = _REPEAT_RE.search(line)
        if m and previous:
            counts[previous] += int(m.group(1))
            continue
        canon = _STREAM_RE.sub("#?:?", _ADDR_RE.sub("@ 0x?", line))
        counts[canon] += 1
        previous = canon
    return counts


def added_decode_errors(ffmpeg: str, out: Path, src: Path, log=None):
    """产物比源文件多出来的解码错误；产物没引入新问题时返回空列表。

    只解产物是不够的。片源自带的坏帧——AVI 里帧边界没对齐的 mp3、损坏的包——
    在音轨 copy 时会原样进到产物里，把它们算成「编码把文件弄坏了」，会让一个
    几小时的正常编码结果被丢掉，而且每次重跑都在同一处翻车。所以产物报错时
    再解一遍源文件当基线，只有源里没有的错误才算产物的问题。

    比对是单向的：源文件全流解码会多出一些产物不可能有的噪音（容器层的 dts
    告警、旧编码器的 flag 修正），那些不该影响判断。

    基线是惰性的：产物解码干净就根本不碰源文件，正常流程零开销。
    """
    out_errs = decode_errors(ffmpeg, out)
    if not out_errs:
        return []
    out_counts = count_decode_errors(out_errs)
    if log:
        log.info(f"[校验] {em(src.name)}：产物解码有 {len(out_errs)} 处报错，"
                 f"解一遍源文件做基线比对")
    src_counts = count_decode_errors(decode_errors(ffmpeg, src))
    added = []
    for msg, n in out_counts.items():
        before = src_counts.get(msg, 0)
        if n <= before:
            continue
        if before:
            added.append(f"{msg}（产物 {n} 次，源文件只有 {before} 次）")
        elif n > 1:
            added.append(f"{msg}（{n} 次）")
        else:
            added.append(msg)
    if not added and log:
        sample = "；".join(list(out_counts)[:2])
        log.warning(f"{em(src.name)}：产物解码报的错源文件本来就有"
                    f"（{sample}），不是编码引入的，按通过处理")
    return added


def check_basic(orig_info, out_info, orig_size: int, new_size: int, cfg):
    max_diff = to_float(cfg["verify"].get("max_duration_diff_s"), 0.5)
    if orig_info.duration > 0 and out_info.duration > 0:
        diff = abs(orig_info.duration - out_info.duration)
        if diff > max_diff:
            return False, f"时长差了 {diff:.2f}s，超过允许的 {max_diff}s"
    if not out_info.video_codec:
        return False, "产物里没有视频流"
    # 分辨率必须一致：滤镜链写错、编码器悄悄降采样都会在这里暴露
    if orig_info.width and orig_info.height and out_info.width and out_info.height:
        if (orig_info.width, orig_info.height) != (out_info.width, out_info.height):
            return False, (
                f"分辨率变了：{orig_info.width}x{orig_info.height} -> "
                f"{out_info.width}x{out_info.height}"
            )
    for kind, before, after in (
        ("音轨", len(orig_info.audio_codecs), len(out_info.audio_codecs)),
        ("字幕", len(orig_info.subtitle_codecs),
         len(out_info.subtitle_codecs)),
    ):
        if before != after:
            return False, f"{kind}条数变了：{before} -> {after}"
    if new_size >= orig_size:
        return False, f"产物 {new_size} 字节，没比源文件 {orig_size} 字节小"
    min_saving = to_float(cfg["verify"].get("min_actual_saving"), 0.05)
    saving = 1.0 - new_size / orig_size
    if saving < min_saving:
        return False, f"实际只省了 {saving:.1%}，低于要求的 {min_saving:.0%}，丢弃产物"
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
            problems.append(f"{label} {before} -> {after or '未标注'}")
    before_hdr = set(getattr(orig_info, "hdr_metadata", ()) or ())
    after_hdr = set(getattr(out_info, "hdr_metadata", ()) or ())
    lost = sorted(before_hdr - after_hdr)
    if lost:
        problems.append("丢了 " + ", ".join(lost))
    if problems:
        return False, "色彩/HDR 元数据变了：" + "；".join(problems)
    return True, ""


def vmaf_model(info, cfg) -> str:
    """按片源分辨率选 VMAF 模型。

    自适应搜索和这里的抽检必须用同一把尺子，所以模型选择只有一个开关
    （[verify] vmaf_4k_model），搜索那边也读它。
    """
    return vmaf.model_for(
        getattr(info, "width", 0), getattr(info, "height", 0),
        bool(cfg["verify"].get("vmaf_4k_model", True)),
    )


def _fmt_segments(records) -> str:
    return ", ".join(f"{s:.2f}@{t:.0f}s" for t, s in records)


def _score_segment(tools, ref: Path, dist: Path, threshold: float,
                   index: int, start: float, log=None, model: str = "") -> float:
    """量一段的 VMAF；分数异常低时先确认不是错帧量出来的假警报。

    抽段边界、VFR 片源、时间戳精度都可能让参考段和待测段差一帧，逐帧比
    出来的分数会崩到 30~60。前后各挪一帧再量，如果分数立刻回到正常水平，
    说明是对齐问题而不是画质问题，取对齐后的分数。
    """
    score = vmaf.measure(tools.ffmpeg, dist, ref, model=model, log=log)
    if score >= threshold:
        return score
    best, best_shift = score, 0
    for shift in (1, -1):
        try:
            alt = vmaf.measure(tools.ffmpeg, dist, ref, shift,
                               model=model, log=log)
        except vmaf.VmafError:
            continue
        if alt > best:
            best, best_shift = alt, shift
    if best_shift and log:
        log.warning(
            f"第 {index} 段（{start:.1f}s 处）只有 VMAF {score:.2f}，"
            f"错开 {best_shift:+d} 帧再量得 {best:.2f}，"
            f"判定为抽样错帧而不是真的画质崩了"
        )
    return best


def vmaf_spot_check(tools, orig: Path, compressed: Path, duration: float,
                    cfg, log=None, fps: float = 0.0,
                    lossless_codec: str = "libx264", model: str = ""):
    """返回 (均值, 最低段, 失败原因)；量不出来时前两个是 None。

    最低段也要带出去：自适应校验想按 target_vmaf 复核，[verify] 的单段下限
    是另一套更松的口径，调用方需要原始数据。
    """
    vcfg = cfg["verify"]
    if not vcfg.get("vmaf_enabled", True):
        return None, None, ""
    segments = max(1, to_int(vcfg.get("vmaf_segments"), 3))
    seconds = to_float(vcfg.get("vmaf_sample_seconds"), 20.0)
    threshold = to_float(vcfg.get("vmaf_threshold"), 93.0)
    if seconds <= 0:
        seconds = 20.0
    if duration <= 0:
        return None, None, "不知道时长，选不了抽样点"

    records = []
    # 两个文件用同一批切点，且切在帧间隙里，抽出的段才逐帧对应
    starts = vmaf.sample_starts(duration, seconds, segments, fps=fps)
    with scratch.temp_dir(prefix="vcvmaf_") as td:
        root = Path(td)
        for i, start in enumerate(starts):
            ref = root / f"ref{i}.mkv"
            dist = root / f"dist{i}.mkv"
            if log:
                log.info(f"[校验] VMAF 抽检第 {i + 1}/{len(starts)} 段")
            try:
                vmaf.extract_segment(tools.ffmpeg, orig, start, seconds, ref,
                                     lossless_codec)
                vmaf.extract_segment(tools.ffmpeg, compressed, start,
                                     seconds, dist, lossless_codec)
                score = _score_segment(tools, ref, dist, threshold, i + 1,
                                       start, log, model)
                if log:
                    log.debug(f"[校验] 第 {i + 1} 段（{start:.1f}s 处）VMAF {score:.2f}")
                records.append((start, score))
            except vmaf.VmafError as e:
                return None, None, str(e)
    if not records:
        return None, None, "一段 VMAF 都没量出来"
    mean = sum(s for _t, s in records) / len(records)
    floor = to_float(vcfg.get("vmaf_min_segment"), 0.0)
    worst_t, worst = min(records, key=lambda r: r[1])
    # 段间取算术平均会掩盖单段崩掉：99/99/81 的均值是 93，照样通过，但那 81 分
    # 的十几分钟是看得出来的。所以除了均值，再给单段设一条下限。
    if floor > 0 and worst < floor:
        return mean, worst, (
            f"{worst_t:.0f}s 处那段只有 VMAF {worst:.2f}，低于单段下限 "
            f"{floor}（各段：{_fmt_segments(records)}）"
        )
    if mean < threshold:
        return mean, worst, (
            f"VMAF 均值 {mean:.2f} 低于阈值 {threshold}"
            f"（各段：{_fmt_segments(records)}）"
        )
    return mean, worst, ""


def run_all(tools, src: Path, out: Path, orig_info, out_info, cfg,
            log=None, lossless_codec: str = "libx264",
            vmaf_available: bool = True):
    """跑完整校验链，任何硬失败抛 VerifyError；返回 (VMAF 均值, 最低段)。

    两个值都可能为 None（这个构建量不了 VMAF 时）。顺序是刻意的：先做便宜的
    （解码、时长、体积、色彩元数据），最后才做最贵的 VMAF 抽检。

    `vmaf_available` 是构建能力（caps 里有没有 libvmaf 滤镜），由调用方给。
    """
    if cfg["verify"].get("decode_check", True):
        errs = added_decode_errors(tools.ffmpeg, out, src, log=log)
        if errs:
            raise VerifyError("解码校验没过：" + "；".join(errs[:3]))

    ok, msg = check_basic(orig_info, out_info, orig_info.size,
                          out.stat().st_size, cfg)
    if not ok:
        raise VerifyError(msg)

    ok, msg = check_color(orig_info, out_info, cfg)
    if not ok:
        raise VerifyError(msg)

    mean, worst, msg = vmaf_spot_check(tools, src, out, orig_info.duration,
                                       cfg, log=log, fps=orig_info.fps,
                                       lossless_codec=lossless_codec,
                                       model=vmaf_model(orig_info, cfg))
    if msg and mean is None:
        # 「量不出来」分两种，后果差了一个量级：
        #
        # 构建里根本没有 libvmaf——每个文件都一样，谁也拿不到分数，硬失败就等于
        # 这个工具在这台机器上不能用。这种降级为告警。
        #
        # 有 libvmaf 却在这个文件上失败——抽样写不下去（临时目录满了）、片段解不
        # 开、libvmaf 中途崩掉。这种必须硬失败：画质是这个工具唯一的承诺，量不出
        # 来就不能说"通过"，尤其是 --delete-original 会跟着把原文件删掉。实际
        # 踩过一次：一个 4K 片源抽到第 3/5 段时抽样失败，其余检查放行，原文件
        # 就这么没了，而全片画质从来没被量过。
        if vmaf_available:
            raise VerifyError(f"量不出 VMAF，不能确认画质（{msg}）")
        if log:
            log.warning(f"{em(src.name)}：这个 ffmpeg 构建没有 libvmaf，"
                        f"量不了画质（{msg}），其余检查已通过，继续")
    elif msg:
        raise VerifyError(msg)
    return mean, worst
