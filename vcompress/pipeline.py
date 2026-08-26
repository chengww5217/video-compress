"""单个文件的处理流程：探测 -> 判定 -> 编码 -> 校验 -> 落地。

每一步都是独立函数，process_file 只负责串起来和统一的失败/中断处理。
"""

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import adaptive, caps, encode, files, probe, proc, tracks, verify
from .caps import Caps
from .logfmt import em
from .progress import ProgressPrinter
from .results import ResultStore
from .util import fmt_size, to_float

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
    def lossless_codec(self) -> str:
        return self.caps.lossless_codec

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

    skip, reason = probe.should_skip(info, ctx.cfg)
    if skip:
        log.info(f"[跳过] {em(src.name)}：{reason}")
        ctx.store.add("skipped", info, reason=reason)
        return ctx.stats.record(SKIPPED)

    if ctx.dry_run:
        log.info(f"[预览] 会压缩 {em(src.name)}"
                 f"（{info.codec_family or info.video_codec}，"
                 f"{fmt_size(info.size)}）")
        return ctx.stats.record(DRYRUN)

    return _compress(ctx, src, info)


def _compress(ctx: RunContext, src: Path, info) -> str:
    tmp = files.tmp_path(src, ctx.out_ext)
    printer = ProgressPrinter(src.name, ctx.show_progress)
    try:
        # 先把输出名定下来（并登记预留），别编码几小时后才发现名字冲突
        target = ((ctx.output_dir / src.name).with_suffix(ctx.out_ext)
                  if ctx.output_dir is not None else src.with_suffix(ctx.out_ext))
        ctx.outputs.claim(src, target, ctx.out_ext, ctx.encoder, ctx.keep)

        crf, elapsed, picked = _encode(ctx, src, info, tmp, printer)
        printer.close()

        out_info = probe.probe(ctx.tools, tmp)
        mean, worst = verify.run_all(ctx.tools, src, tmp, info, out_info,
                                     ctx.cfg, log=log,
                                     lossless_codec=ctx.lossless_codec)
        if mean is not None:
            log.info(f"[校验] {em(src.name)}：VMAF {em(f'{mean:.2f}')}")
        _warn_below_target(ctx, src, picked, mean, worst)

        saved = _commit(ctx, src, tmp, target, info, crf, elapsed)
        return ctx.stats.record(OK, saved)
    except SkipFile as skipped:
        # 搜索阶段就看出不值得压：删掉可能留下的半成品，按跳过记账
        printer.close()
        files.remove_tmp(tmp)
        log.info(f"[跳过] {em(src.name)}：{skipped}")
        ctx.store.add("skipped", info, reason=str(skipped))
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


def _warn_below_target(ctx: RunContext, src: Path, picked, mean, worst) -> None:
    """自适应产物按 target_vmaf 复核全片抽检，低于目标必须留痕。

    [verify] 的阈值是给固定 CRF 兜底的灾难性失败线；自适应承诺的是
    target_vmaf。两边抽样位置不同，搜索达标不代表全片达标。产物仍按通过
    处理（它过了 [verify] 的闸门），但画质的欠账不能无声无息。
    """
    if picked is None or mean is None:
        return
    target = to_float(ctx.cfg["adaptive"].get("target_vmaf"), 95.0)
    low = []
    if mean < target:
        low.append(f"均值 {mean:.2f}")
    if worst is not None and worst < target:
        low.append(f"最低段 {worst:.2f}")
    if not low:
        return
    log.warning(
        f"[校验] {em(src.name)}：全片抽检 VMAF 没达到自适应目标 "
        f"{em(str(target))}（{'、'.join(low)}；搜索时样本最差 "
        f"{picked.worst_vmaf:.2f}）。产物仍按通过保留，"
        f"介意的话删掉产物加 --force 重压"
    )


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


def _commit(ctx: RunContext, src: Path, tmp: Path, target: Path, info,
            crf: float, elapsed: float) -> int:
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
        f"{em(fmt_size(new_size))}，用了 {elapsed:.0f}s{kept}）"
    )
    ctx.store.add("ok", info, output=str(final), crf=crf, new_size=new_size,
                  saving=round(saving, 4), seconds=round(elapsed, 1),
                  kept=ctx.keep)
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
    kept_tmp = None
    if ctx.cfg["verify"].get("keep_failed_output", False):
        kept_tmp = files.keep_failed_output(tmp, src, ctx.out_ext)
    ctx.store.add("failed", info, stage="process", error=str(exc)[:500],
                  failed_output=str(kept_tmp) if kept_tmp else None)
    if kept_tmp is None:
        files.remove_tmp(tmp)
    return ctx.stats.record(FAILED)
