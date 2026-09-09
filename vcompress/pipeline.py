"""单个文件的处理流程：探测 -> 判定 -> 编码 -> 校验 -> 落地。

每一步都是独立函数，process_file 只负责串起来和统一的失败/中断处理。
"""

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import adaptive, caps, encode, files, probe, proc, remux, tracks, verify
from .caps import Caps
from .logfmt import em
from .progress import ProgressPrinter
from .results import ResultStore
from .util import fmt_hms, fmt_size, to_float

log = logging.getLogger("vcompress")

OK = "ok"
SKIPPED = "skipped"
FAILED = "failed"
DRYRUN = "dryrun"
ABORTED = "aborted"
OUTCOMES = (OK, SKIPPED, FAILED, DRYRUN, ABORTED)


class RunStats:
    """本次运行的汇总。并发下由多个线程写，所以带锁。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.counts = Counter({o: 0 for o in OUTCOMES})
        self.saved_bytes = 0

    def record(self, outcome: str, saved: int = 0) -> str:
        with self._lock:
            self.counts[outcome] += 1
            self.saved_bytes += max(0, saved)
        return outcome


@dataclass(frozen=True)
class RunContext:
    """一次运行里所有文件共享的、只读的执行环境。

    以前是 `class Ctx: pass` 加十来处动态赋值——属性名拼错要到运行时才炸，
    也没法一眼看出一个 worker 到底依赖了什么。
    """

    cfg: dict
    tools: probe.Tools
    caps: Caps
    store: ResultStore
    outputs: files.OutputRegistry
    stats: RunStats = field(default_factory=RunStats)
    encoder: str = "av1"
    dry_run: bool = False
    adaptive: bool = True
    recycle: bool = False
    keep: bool = False
    show_progress: bool = False
    # 重封装模式：修「不用再压但快进卡顿」的文件，见 remux.py
    remux: bool = False
    # None = 源文件所在目录；指定后所有产物落到这个目录（-o/--output）
    output_dir: "Path | None" = None

    @property
    def enc_cfg(self) -> dict:
        return self.cfg["encode"][self.encoder]

    @property
    def out_ext(self) -> str:
        return self.cfg["encode"]["output_container"]

    @property
    def marker(self) -> str:
        return self.cfg["skip"]["tag_marker"] + "-v1"

    @property
    def remux_marker(self) -> str:
        """重封装产物的标记。含普通标记（vcompress-remux-v1 里有 vcompress），
        所以压缩模式靠 marker 检查就能跳过它们；重封装模式自己再认这个更具体
        的标记。"""
        return self.cfg["skip"]["tag_marker"] + "-remux-v1"

    @property
    def mode(self) -> str:
        """写进 results.jsonl 的模式字段，缓存命中要求模式一致。"""
        return "remux" if self.remux else "compress"

    @property
    def lossless_codec(self) -> str:
        return self.caps.lossless_codec

    @property
    def vmaf_available(self) -> bool:
        """这个 ffmpeg 构建能不能量 VMAF。

        校验链要用它区分「谁都量不了」（降级告警）和「这个文件量不出来」
        （硬失败）。构建能力问不出来时（caps 为空）按能用算，让实际的
        失败信息说话。
        """
        return not self.caps.filters or "libvmaf" in self.caps.filters

    @property
    def adaptive_enabled(self) -> bool:
        return self.adaptive and bool(self.cfg["adaptive"].get("enabled", True))


class SkipFile(Exception):
    """编码开始前就断定不该压：按跳过记账，不算失败。"""


def process_file(ctx: RunContext, src: Path) -> str:
    try:
        info = probe.probe(ctx.tools, src)
    except Exception as e:
        log.error(f"[失败] {em(src.name)}：探测失败：{e}")
        if not ctx.dry_run:
            # 预览不是真实运行：失败要报（真跑也会失败），但不能落进
            # results.jsonl——那是断点续跑的状态，预览不该留下状态
            ctx.store.add("failed", None, path=str(src.resolve()),
                          stage="probe", error=str(e)[:500])
        return ctx.stats.record(FAILED)

    if ctx.remux:
        return _process_remux(ctx, src, info)

    skip, reason = probe.should_skip(info, ctx.cfg)
    if skip:
        log.info(f"[跳过] {em(src.name)}：{reason}")
        ctx.store.add("skipped", info, reason=reason, mode=ctx.mode)
        return ctx.stats.record(SKIPPED)

    if ctx.dry_run:
        log.info(f"[预览] 会压缩 {em(src.name)}"
                 f"（{info.codec_family or info.video_codec}，"
                 f"{fmt_size(info.size)}）")
        return ctx.stats.record(DRYRUN)

    return _compress(ctx, src, info)


def _remux_skip(ctx: RunContext, info, reason: str) -> str:
    log.info(f"[跳过] {em(info.path.name)}：{reason}")
    ctx.store.add("skipped", info, reason=reason, mode=ctx.mode)
    return ctx.stats.record(SKIPPED)


def _process_remux(ctx: RunContext, src: Path, info) -> str:
    """重封装模式的入口：先按自己的口径筛，再把「不用再压」的送去体检。

    should_skip 的五类结论在这里分两拨：标记/BPP/预估收益都是「这个文件已经
    很高效」——正是重封装的目标客户；交错和太小是「别碰它」——照旧跳过。
    交错和大小在这里先查一遍，剩下的 skip 结论就只可能是「已高效」那一拨，
    不用去解析 should_skip 返回的字符串。
    """
    comment = (info.comment or "").lower()
    if ctx.remux_marker.lower() in comment:
        return _remux_skip(ctx, info, "元数据里已有重封装标记，之前处理过了")
    if ctx.cfg["skip"].get("skip_interlaced", True) and info.interlaced:
        return _remux_skip(
            ctx, info,
            f"交错源（field_order={info.field_order}）：修关键帧要重编码，"
            f"会丢交错标记，不碰")
    min_size = int(ctx.cfg["skip"].get("min_size_mb", 0)) * 1024 * 1024
    if info.size and info.size < min_size:
        return _remux_skip(ctx, info, f"文件太小（{info.size // 1024 // 1024} MB）")

    skip, _reason = probe.should_skip(info, ctx.cfg)
    if skip:
        # 标记/BPP/预估收益三类：已经高效，去查快进友好性
        return _remux_analyze(ctx, src, info)

    if ctx.dry_run:
        log.info(f"[预览] 会压缩 {em(src.name)}"
                 f"（{info.codec_family or info.video_codec}，"
                 f"{fmt_size(info.size)}，关键帧间隔按重封装模式收紧）")
        return ctx.stats.record(DRYRUN)
    # 该压的照常压，只是 keyint 也按间隔上限收紧，一遍跑完整库都快进友好
    return _compress(ctx, src, info)


def _remux_analyze(ctx: RunContext, src: Path, info) -> str:
    """体检：量关键帧间隔，决定重编码 / 换容器 / 不动。"""
    try:
        stamps = remux.keyframe_timestamps(ctx.tools, src)
    except Exception as e:
        return _remux_skip(ctx, info, f"扫关键帧失败：{e}")
    if not stamps:
        return _remux_skip(ctx, info, "读不到任何关键帧时间戳，不敢动")

    gap_s = to_float(ctx.cfg["remux"].get("max_keyframe_gap_s"), 5.0)
    max_gap = remux.max_keyframe_gap(stamps, info.duration)
    plan = remux.plan_remux(info, ctx.cfg, max_gap)

    if not plan.strategy:
        return _remux_skip(ctx, info, plan.reason)

    if ctx.dry_run:
        log.info(f"[预览] 会重封装 {em(src.name)}：{remux.describe(plan, gap_s)}")
        return ctx.stats.record(DRYRUN)

    if plan.strategy == remux.GOP:
        return _remux_gop(ctx, src, info, plan)
    return _remux_copy(ctx, src, info, plan)


def _compress(ctx: RunContext, src: Path, info) -> str:
    tmp = files.tmp_path(src, ctx.out_ext)
    printer = ProgressPrinter(src.name, ctx.show_progress)
    try:
        # 先把输出名定下来（并登记预留），别编码几小时后才发现名字冲突
        target = files.output_name(
            ctx.output_dir / src.name if ctx.output_dir is not None else src,
            ctx.encoder, ctx.out_ext)
        ctx.outputs.claim(src, target, ctx.out_ext, ctx.encoder, ctx.keep)

        crf, elapsed, picked = _encode(ctx, src, info, tmp, printer)
        printer.close()

        out_info = probe.probe(ctx.tools, tmp)
        mean, worst = verify.run_all(ctx.tools, src, tmp, info, out_info,
                                     ctx.cfg, log=log,
                                     lossless_codec=ctx.lossless_codec,
                                     vmaf_available=ctx.vmaf_available)
        if mean is not None:
            log.info(f"[校验] {em(src.name)}：VMAF {em(f'{mean:.2f}')}")
        below = _warn_below_target(ctx, src, picked, mean, worst)

        saved = _commit(ctx, src, tmp, target, info, crf, elapsed,
                        mean, worst, below)
        return ctx.stats.record(OK, saved)
    except SkipFile as skipped:
        # 搜索阶段就看出不值得压：删掉可能留下的半成品，按跳过记账
        printer.close()
        files.remove_tmp(tmp)
        log.info(f"[跳过] {em(src.name)}：{skipped}")
        ctx.store.add("skipped", info, reason=str(skipped), mode=ctx.mode)
        return ctx.stats.record(SKIPPED)
    except BaseException as exc:  # noqa: BLE001 - 中断也要走清理
        printer.close()
        return _handle_failure(ctx, src, info, tmp, exc)
    finally:
        ctx.outputs.release(src)


def _encode(ctx: RunContext, src: Path, info, tmp: Path,
            printer: ProgressPrinter):
    """跑主编码，返回 (实际使用的 crf, 耗时秒, 搜索结论或 None)。"""
    supported = caps.encoder_pix_fmts(ctx.tools.ffmpeg, ctx.enc_cfg["codec"])
    enc_cfg = caps.with_pix_fmt(ctx.enc_cfg, info.pix_fmt, supported, log)
    if ctx.remux:
        # 重封装模式下「该压的照常压」，但 keyint 一并收紧——不然这次压完
        # 下次还得为快进卡顿再重编一遍
        gap_s = to_float(ctx.cfg["remux"].get("max_keyframe_gap_s"), 5.0)
        enc_cfg = remux.bound_keyint_cfg(enc_cfg, info.fps, gap_s)

    # 固定 CRF 也走一遍量化：配置里写 20.5 就该按 20.5 编，而不是被截成 20
    crf = encode.quantize_crf(to_float(enc_cfg["crf"], 28))
    picked = None
    if ctx.adaptive_enabled and info.duration > 0:
        picked = adaptive.pick_crf(
            ctx.tools, src, info.duration, enc_cfg, ctx.cfg["adaptive"], log,
            ctx.lossless_codec, verify.vmaf_model(info, ctx.cfg),
        )
        if picked is not None:
            crf = picked.crf
            _reject_unworthy(ctx, info, picked)

    cmd = encode.build_cmd(
        ctx.tools.ffmpeg, src, tmp, enc_cfg, crf,
        tracks.audio_args(info, ctx.cfg, ctx.out_ext),
        tracks.subtitle_args(info, ctx.out_ext),
        ctx.marker,
        info.comment,
        encode.color_args(info),
    )
    log.info(f"[编码] {em(src.name)}：crf={em(encode.fmt_crf(crf))}"
             f"（{ctx.encoder}）")
    printer(0.0)
    t0 = time.monotonic()
    encode.run_encode(cmd, info.duration, printer)
    elapsed = time.monotonic() - t0
    printer(100.0)
    return crf, elapsed, picked


def _warn_below_target(ctx: RunContext, src: Path, picked, mean, worst,
                       target=None, label: str = "自适应目标"):
    """自适应产物按 target_vmaf 复核全片抽检，低于目标必须留痕。

    [verify] 的阈值是给固定 CRF 兜底的灾难性失败线；自适应承诺的是
    target_vmaf。搜索是在几十秒样本上做的，全片抽检看的是另一批更长的片段，
    搜索达标不代表全片达标。产物仍按通过处理（它过了 [verify] 的闸门），但
    画质的欠账不能无声无息。

    返回留痕用的文字（没欠账时返回空串），由调用方写进 results.jsonl——只打
    日志的话，想知道"哪些文件没达到目标"就只能去 grep 几万行日志。
    """
    if picked is None or mean is None:
        return ""
    if target is None:
        target = to_float(ctx.cfg["adaptive"].get("target_vmaf"), 95.0)
    low = []
    if mean < target:
        low.append(f"均值 {mean:.2f}")
    if worst is not None and worst < target:
        low.append(f"最低段 {worst:.2f}")
    if not low:
        return ""
    detail = "、".join(low)
    log.warning(
        f"[校验] {em(src.name)}：全片抽检 VMAF 没达到{label} "
        f"{em(str(target))}（{detail}；搜索时样本最差 "
        f"{picked.worst_vmaf:.2f}）。产物仍按通过保留，"
        f"介意的话删掉产物加 --force 重压"
    )
    return f"{detail}（目标 {target}，搜索时样本最差 {picked.worst_vmaf:.2f}）"


def _reject_unworthy(ctx: RunContext, info, picked) -> None:
    """搜索结果已经能回答两个问题，别等几小时后才知道答案。

    1. 这个片源根本达不到目标画质吗？达不到就不该压——「视觉无损优先」的前提下，
       压出一个低于 target_vmaf 的产物没有意义；
    2. 按样本实测码率，压完能省多少？省得不够就不压。这比 expected_ratio 那张
       按编码器写死的粗估准得多，而且是在动手编码之前。
    """
    if not picked.meets_target and ctx.cfg["adaptive"].get("require_target", True):
        raise SkipFile(
            f"连 crf={encode.fmt_crf(picked.crf)} 都只有 VMAF "
            f"{picked.worst_vmaf:.2f}，低于目标 "
            f"{ctx.cfg['adaptive']['target_vmaf']}，不值得压"
        )
    predicted = tracks.predicted_size(info, ctx.cfg,
                                      picked.video_bitrate, ctx.out_ext)
    if predicted is None or info.size <= 0:
        return
    saving = 1.0 - predicted / info.size
    floor = float(ctx.cfg["skip"].get("min_saving_ratio", 0.15))
    log.info(f"[预估] {em(info.path.name)}：crf={em(encode.fmt_crf(picked.crf))}，"
             f"样本实测 {picked.video_bitrate // 1000} kbps，预计压到 "
             f"~{fmt_size(predicted)}（省 {em(f'{saving:.1%}')}）")
    if saving < floor:
        raise SkipFile(
            f"按 crf={encode.fmt_crf(picked.crf)} 的样本码率预计只压到 "
            f"{fmt_size(predicted)}（省 {saving:.1%}），低于要求的 {floor:.0%}"
        )


def _remux_gop(ctx: RunContext, src: Path, info, plan) -> str:
    """GOP 修复：只重编码视频轨，音轨/字幕/章节原样复制。

    和压缩流程同构但口径不同：画质闸门用 [remux] target_vmaf（更严，二次
    编码要留代际余量），体积闸门反向（允许涨到 max_growth_ratio），音轨一律
    复制。搜索失败不回退固定 CRF——压缩模式回退了顶多画质差点，这里回退
    等于拿一个没量过画质的二次编码去替换原本完好的文件。
    """
    tmp = files.tmp_path(src, ctx.out_ext)
    printer = ProgressPrinter(src.name, ctx.show_progress)
    try:
        base = (ctx.output_dir / src.name
                if ctx.output_dir is not None else src)
        target = files.output_name(base, ctx.encoder, ctx.out_ext)
        ctx.outputs.claim(src, target, ctx.out_ext, ctx.encoder, ctx.keep)

        gap_s = to_float(ctx.cfg["remux"].get("max_keyframe_gap_s"), 5.0)
        growth_cap = to_float(ctx.cfg["remux"].get("max_growth_ratio"), 0.10)
        supported = caps.encoder_pix_fmts(ctx.tools.ffmpeg,
                                          ctx.enc_cfg["codec"])
        enc_cfg = caps.with_pix_fmt(ctx.enc_cfg, info.pix_fmt, supported, log)
        enc_cfg = remux.bound_keyint_cfg(enc_cfg, info.fps, gap_s)

        if not (ctx.adaptive_enabled and info.duration > 0):
            raise SkipFile(
                "没有自适应搜索就定不了重编的画质（--no-adaptive 或读不出"
                "时长），不动这个文件"
            )
        picked = adaptive.pick_crf(
            ctx.tools, src, info.duration, enc_cfg,
            remux.adaptive_cfg(ctx.cfg), log,
            ctx.lossless_codec, verify.vmaf_model(info, ctx.cfg),
        )
        if picked is None:
            raise SkipFile(
                "自适应搜索失败，重封装不回退固定 CRF（二次编码压坏了没有"
                "兜底），不动这个文件"
            )
        if not picked.meets_target:
            raise SkipFile(
                f"连 crf={encode.fmt_crf(picked.crf)} 都只有 VMAF "
                f"{picked.worst_vmaf:.2f}，达不到重封装目标 "
                f"{ctx.cfg['remux']['target_vmaf']}，不动它更安全"
            )

        predicted = remux.predicted_size(info, picked.video_bitrate)
        limit = remux.growth_limit(info.size, ctx.cfg)
        if predicted is not None and info.size > 0:
            growth = predicted / info.size - 1
            log.info(f"[预估] {em(src.name)}：crf="
                     f"{em(encode.fmt_crf(picked.crf))}，keyint="
                     f"{em(str(plan.keyint))}，样本实测 "
                     f"{picked.video_bitrate // 1000} kbps，预计 "
                     f"~{fmt_size(predicted)}（{growth:+.1%}）")
            if predicted > limit:
                raise SkipFile(
                    f"按样本码率预计压到 {fmt_size(predicted)}"
                    f"（比源文件大 {growth:.1%}），超过允许的 "
                    f"{growth_cap:.0%} 上限，不动它"
                )

        log.info(f"[重封装] {em(src.name)}：重编码修关键帧间隔"
                 f"（{plan.reason}），crf={em(encode.fmt_crf(picked.crf))}，"
                 f"keyint={em(str(plan.keyint))}（{ctx.encoder}）")
        printer(0.0)
        cmd = encode.build_cmd(
            ctx.tools.ffmpeg, src, tmp, enc_cfg, picked.crf,
            ["-c:a", "copy"],
            tracks.subtitle_args(info, ctx.out_ext),
            ctx.remux_marker,
            info.comment,
            encode.color_args(info),
        )
        t0 = time.monotonic()
        encode.run_encode(cmd, info.duration, printer)
        elapsed = time.monotonic() - t0
        printer(100.0)

        out_info = probe.probe(ctx.tools, tmp)
        out_gap = remux.max_keyframe_gap(
            remux.keyframe_timestamps(ctx.tools, tmp), out_info.duration)
        if out_gap > gap_s:
            # 编码器没兑现 keyint 承诺就是白干一场，宁可判失败也不交付一个
            # 「修了但没修好」的产物
            raise verify.VerifyError(
                f"重编码后关键帧最长间隔仍有 {out_gap:.1f}s，超过 "
                f"{gap_s:.0f}s 的目标，keyint 参数没有生效"
            )
        mean, worst = verify.run_all(ctx.tools, src, tmp, info, out_info,
                                     ctx.cfg, log=log,
                                     lossless_codec=ctx.lossless_codec,
                                     vmaf_available=ctx.vmaf_available,
                                     max_growth_ratio=growth_cap)
        if mean is not None:
            log.info(f"[校验] {em(src.name)}：VMAF {em(f'{mean:.2f}')}")
        below = _warn_below_target(
            ctx, src, picked, mean, worst,
            target=to_float(ctx.cfg["remux"].get("target_vmaf"), 98.0),
            label="重封装目标")

        _remux_commit(ctx, src, tmp, target, info, plan, ctx.encoder,
                      crf=picked.crf, elapsed=elapsed, vmaf_mean=mean,
                      vmaf_worst=worst, below_target=below)
        return ctx.stats.record(OK)
    except SkipFile as skipped:
        printer.close()
        files.remove_tmp(tmp)
        log.info(f"[跳过] {em(src.name)}：{skipped}")
        ctx.store.add("skipped", info, reason=str(skipped), mode=ctx.mode)
        return ctx.stats.record(SKIPPED)
    except BaseException as exc:  # noqa: BLE001 - 中断也要走清理
        printer.close()
        return _handle_failure(ctx, src, info, tmp, exc)
    finally:
        ctx.outputs.release(src)


def _remux_copy(ctx: RunContext, src: Path, info, plan) -> str:
    """流复制换容器：画面逐字节不变，只修容器层的 seek 索引。"""
    tmp = files.tmp_path(src, ctx.out_ext)
    printer = ProgressPrinter(src.name, ctx.show_progress)
    try:
        base = (ctx.output_dir / src.name
                if ctx.output_dir is not None else src)
        target = files.output_name(base, "remux", ctx.out_ext)
        ctx.outputs.claim(src, target, ctx.out_ext, "remux", ctx.keep)

        log.info(f"[重封装] {em(src.name)}：流复制换容器（{plan.reason}），"
                 f"画面逐字节不变")
        printer(0.0)
        cmd = encode.build_copy_cmd(
            ctx.tools.ffmpeg, src, tmp,
            tracks.subtitle_args(info, ctx.out_ext),
            ctx.remux_marker, info.comment,
        )
        t0 = time.monotonic()
        encode.run_encode(cmd, info.duration, printer)
        elapsed = time.monotonic() - t0
        printer(100.0)

        out_info = probe.probe(ctx.tools, tmp)
        remux.verify_copy(ctx.tools, src, tmp, info, out_info, ctx.cfg,
                          log=log)
        _remux_commit(ctx, src, tmp, target, info, plan, "remux",
                      elapsed=elapsed)
        return ctx.stats.record(OK)
    except SkipFile as skipped:
        printer.close()
        files.remove_tmp(tmp)
        log.info(f"[跳过] {em(src.name)}：{skipped}")
        ctx.store.add("skipped", info, reason=str(skipped), mode=ctx.mode)
        return ctx.stats.record(SKIPPED)
    except BaseException as exc:  # noqa: BLE001 - 中断也要走清理
        printer.close()
        return _handle_failure(ctx, src, info, tmp, exc)
    finally:
        ctx.outputs.release(src)


def _remux_commit(ctx: RunContext, src: Path, tmp: Path, target: Path, info,
                  plan, tag: str, crf=None, elapsed: float = 0.0,
                  vmaf_mean=None, vmaf_worst=None, below_target: str = ""):
    """重封装产物落地。和 _commit 的差别：涨了也算成功（承诺是「最多涨
    一点点」而不是「必须变小」），记录里带 strategy 和 growth。"""
    final = ctx.outputs.claim(src, target, ctx.out_ext, tag, ctx.keep, log)
    in_place = not ctx.keep and files.same_path(final, src)
    if in_place and ctx.recycle:
        files.trash(src)
    files.move_file(tmp, final)
    if not ctx.keep and not in_place:
        files.delete_original(src, ctx.recycle)

    new_size = final.stat().st_size
    growth = (new_size / info.size - 1.0) if info.size else 0.0
    what = "GOP 修复" if plan.strategy == remux.GOP else "流复制换容器"
    crf_txt = f"，crf={em(encode.fmt_crf(crf))}" if crf is not None else ""
    kept = "，原文件保留" if ctx.keep else ""
    log.info(
        f"[完成] {em(src.name)}：重封装（{what}{crf_txt}），体积 "
        f"{em(f'{growth:+.1%}')}（{fmt_size(info.size)} -> "
        f"{em(fmt_size(new_size))}，用了 {fmt_hms(elapsed)}{kept}）"
    )
    ctx.store.add("ok", info, output=str(final), mode=ctx.mode,
                  strategy=plan.strategy,
                  crf=crf, new_size=new_size, growth=round(growth, 4),
                  seconds=round(elapsed, 1), kept=ctx.keep,
                  vmaf=round(vmaf_mean, 2) if vmaf_mean is not None else None,
                  vmaf_worst=(round(vmaf_worst, 2)
                              if vmaf_worst is not None else None),
                  below_target=below_target or None)
    return max(0, info.size - new_size)


def _commit(ctx: RunContext, src: Path, tmp: Path, target: Path, info,
            crf: float, elapsed: float, vmaf_mean=None, vmaf_worst=None,
            below_target: str = "") -> int:
    """把临时产物换成正式文件并按配置处理原文件，返回省下的字节数。

    输出名在编码前已经定过一次，这里再定一次：几小时里目标名可能被别的东西
    占了，宁可改名也不能覆盖。
    """
    final = ctx.outputs.claim(src, target, ctx.out_ext, ctx.encoder, ctx.keep,
                             log)
    in_place = not ctx.keep and files.same_path(final, src)
    if in_place and ctx.recycle:
        # 原地替换时先把原文件送进回收站，os.replace 才不会直接毁掉它
        files.trash(src)
    files.move_file(tmp, final)  # 输出目录可能在另一块盘上，os.replace 会垮
    if not ctx.keep and not in_place:
        files.delete_original(src, ctx.recycle)

    new_size = final.stat().st_size
    saved = max(0, info.size - new_size)
    saving = saved / info.size if info.size else 0.0
    kept = "，原文件保留" if ctx.keep else ""
    log.info(
        f"[完成] {em(src.name)}：crf={encode.fmt_crf(crf)}，省下 "
        f"{em(f'{saving:.1%}')}（{fmt_size(info.size)} -> "
        f"{em(fmt_size(new_size))}，用了 {fmt_hms(elapsed)}{kept}）"
    )
    ctx.store.add("ok", info, output=str(final), crf=crf, new_size=new_size,
                  saving=round(saving, 4), seconds=round(elapsed, 1),
                  kept=ctx.keep, mode=ctx.mode,
                  # 画质记进去才查得到：光打日志的话，「哪些文件没达到目标」
                  # 只能靠 grep 几万行日志
                  vmaf=round(vmaf_mean, 2) if vmaf_mean is not None else None,
                  vmaf_worst=(round(vmaf_worst, 2)
                              if vmaf_worst is not None else None),
                  below_target=below_target or None)
    return saved


def _handle_failure(ctx: RunContext, src: Path, info, tmp: Path,
                    exc: BaseException) -> str:
    """统一的失败/中断收尾。

    中断必须和普通失败分开处理：Ctrl+C 时不该把半成品留成 .failed.mkv，也不该
    记一条 failed；而且 KeyboardInterrupt 要继续往上抛，否则串行模式下主循环
    会接着处理下一个文件。
    """
    interrupted = isinstance(exc, KeyboardInterrupt) or proc.is_cancelled()
    if interrupted:
        # 串行模式下信号只打到主线程，子进程还活着，这里补一刀
        proc.terminate_all()
        files.remove_tmp(tmp)
        log.info(f"[中止] {em(src.name)}：已被中断")
        if isinstance(exc, BaseException) and not isinstance(exc, Exception):
            raise exc
        return ctx.stats.record(ABORTED)

    if not isinstance(exc, Exception):
        raise exc  # SystemExit / MemoryError 这类不该被当成“这个文件失败了”

    log.error(f"[失败] {em(src.name)}：{exc}")
    # 结论由片源和配置决定的失败，记一笔「不必重试」：不然下次跑同一个目录，
    # 又是一个多小时搜索加几小时编码，换来同一句话
    permanent = isinstance(exc, verify.PermanentVerifyError)
    if permanent:
        log.info(f"{em(src.name)}：这个结论重跑也一样，下次会直接跳过"
                 f"（想再试一次加 {em('--force')}）")
    kept_tmp = None
    if ctx.cfg["verify"].get("keep_failed_output", False):
        kept_tmp = files.keep_failed_output(tmp, src, ctx.out_ext)
    ctx.store.add("failed", info, stage="process", error=str(exc)[:500],
                  permanent=permanent or None, mode=ctx.mode,
                  failed_output=str(kept_tmp) if kept_tmp else None)
    if kept_tmp is None:
        files.remove_tmp(tmp)
    return ctx.stats.record(FAILED)
