"""命令行入口：参数解析、构建自检、调度与汇总。"""

import argparse
import logging
import logging.handlers
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import config, encode, files, logfmt, pipeline, power, probe, proc, results
from .caps import Caps
from .logfmt import em
from .pipeline import RunContext, RunStats
from .results import ResultStore
from .util import fmt_hms, fmt_size

log = logging.getLogger("vcompress")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config.toml"


def setup_logging(log_file: Path, max_bytes: int = 5_242_880,
                  backups: int = 3, argv=None) -> None:
    """控制台 INFO（带颜色）、文件 DEBUG（纯文本 + 会话分隔）。

    文件按大小轮转，长期跑不会无限膨胀。
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logfmt.ConsoleFormatter(logfmt.supports_color(sys.stdout)))
    logfmt.write_session_banner(log_file, argv)
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max(0, int(max_bytes)),
        backupCount=max(0, int(backups)), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logfmt.FileFormatter())
    root.addHandler(ch)
    root.addHandler(fh)


def preflight(caps: Caps, cfg: dict, encoder: str,
              adaptive: bool) -> bool:
    """先确认这个 ffmpeg 构建能干活，别等编码到一半才报错。"""
    codec = cfg["encode"][encoder]["codec"]
    if not caps.known:
        log.warning("问不出这个 ffmpeg 支持哪些编码器，跳过构建自检")
        return True
    if codec not in caps.encoders:
        log.error(f"这个 ffmpeg 构建里没有 {em(codec)}（--encoder {encoder} 需要"
                  f"它）：换个完整构建，或者换编码器")
        return False
    if "libopus" not in caps.encoders:
        log.warning("这个 ffmpeg 构建里没有 libopus，无损轨和超高码率音轨无法转码")

    wants_vmaf = (adaptive and cfg["adaptive"].get("enabled", True)) or \
        cfg["verify"].get("vmaf_enabled", True)
    if wants_vmaf:
        if "libvmaf" not in caps.filters:
            log.warning("这个 ffmpeg 构建里没有 libvmaf 滤镜，自适应 CRF 和 VMAF "
                        "抽检都会退化")
        elif caps.lossless_codec != "libx264":
            log.warning(f"这个 ffmpeg 构建里没有 libx264，抽样改用 "
                        f"{caps.lossless_codec}（临时文件更大）")
    return True


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="vcompress",
        description="基于 ffmpeg 的视觉无损视频压缩工具",
    )
    ap.add_argument("paths", nargs="+", help="要处理的视频文件或目录")
    ap.add_argument("-o", "--output", default=None, metavar="目录",
                    help="输出目录（默认：源文件所在目录）。目录必须已存在，"
                         "有问题会直接报错退出")
    ap.add_argument("--config", default=None,
                    help=f"配置文件（默认：{DEFAULT_CONFIG}）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只预览，不改任何文件")
    ap.add_argument("--force", action="store_true",
                    help="忽略缓存，重新处理之前已跳过/已完成的文件")
    ap.add_argument("--no-recursive", dest="recursive", action="store_false",
                    help="不递归子目录")
    ap.add_argument("--workers", type=int, default=None,
                    help="并发处理的文件数")
    # 候选值来自配置里的 [encode.*] 子表，不在这里硬编码
    ap.add_argument("--encoder", default=None,
                    help="覆盖 [encode] encoder，名字必须有对应的 "
                         "[encode.<名字>] 配置节")
    ap.add_argument("--no-adaptive", dest="adaptive", action="store_false",
                    help="关闭自适应 CRF 搜索，直接用固定 CRF")
    ap.add_argument("--recycle", action="store_true",
                    help="删除原文件时进回收站")
    keep = ap.add_mutually_exclusive_group()
    keep.add_argument("--keep", action="store_true",
                      help="保留原文件，只产出压缩副本（重名时输出改为 "
                           "<名字>.<编码器><扩展名>，仍然重名就报错跳过而不是"
                           "覆盖；这是默认行为）")
    keep.add_argument("--delete-original", action="store_true",
                      help="压缩副本校验通过后删除原文件（覆盖默认的保留行为）")
    return ap


def _check_output_dir(value):
    """-o/--output 的输出目录：有问题（不存在/不是目录）直接告警返回 None。

    目录有问题时继续跑等于把产物落到用户没预期的地方——和当初「第二个参数
    被静默忽略」是同一类事故，所以宁可直接退出。不存在就不帮忙建：建错了
    地方更隐蔽。
    """
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_dir():
        log.error(f"输出目录不存在或不是目录：{path}")
        return None
    return path.resolve()


def _load_config(arg_value):
    """加载配置，并给出 [run] 里相对路径的基准目录。

    --config 显式给了就必须存在；用默认路径时文件缺失回落到内置默认值。
    没有配置文件可依附时（打包安装的场景），相对路径按当前工作目录解析——
    绝不能往 site-packages 里写日志。
    """
    explicit = arg_value is not None
    path = Path(arg_value).expanduser().resolve() if explicit else DEFAULT_CONFIG
    cfg = config.load(path, required=explicit)
    base = path.parent if path.exists() else Path.cwd()
    return cfg, path, base


def _pick_todo(files_found, store: ResultStore, force: bool) -> list:
    todo = []
    for f in files_found:
        cached = None if force else store.is_cached(f)
        if cached:
            log.info(f"[缓存] {em(f.name)}：上次的结果是"
                     f"{results.action_label(cached['action'])}，本次跳过"
                     f"（要重跑加 --force）")
            continue
        todo.append(f)
    return todo


def _run_serial(ctx: RunContext, todo) -> None:
    for f in todo:
        pipeline.process_file(ctx, f)


def _run_parallel(ctx: RunContext, todo, workers: int) -> None:
    # 不能用 with：KeyboardInterrupt 会先触发 shutdown(wait=True)，
    # 卡在等编码结束上，terminate_all 永远排不到。
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [ex.submit(pipeline.process_file, ctx, f) for f in todo]
        for fut in as_completed(futures):
            fut.result()
    except BaseException:
        proc.terminate_all()
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown(wait=True)


def _report(stats: RunStats, store: ResultStore, elapsed: float = 0.0) -> None:
    counts = stats.counts
    parts = [f"压缩 {em(counts['ok'])} 个", f"跳过 {counts['skipped']} 个",
             f"失败 {em(counts['failed']) if counts['failed'] else 0} 个"]
    if counts["dryrun"]:
        parts.append(f"预览 {counts['dryrun']} 个")
    if counts["aborted"]:
        parts.append(f"中止 {counts['aborted']} 个")
    tail = f"，耗时 {fmt_hms(elapsed)}" if elapsed else ""
    summary = "[汇总] 本次运行：" + "、".join(parts) + tail
    # 有失败就别用一片绿色的「完成」糊过去
    if counts["failed"]:
        log.warning(summary + "，失败的详情见上面的错误和 results.jsonl")
    else:
        log.info(summary)
    if counts["ok"]:
        log.info(f"[汇总] 省下 {em(fmt_size(stats.saved_bytes))}，"
                 f"累计省下 {em(fmt_size(store.total_saved()))}"
                 f"（记录在 {store.path}）")


def _log_settings(cfg: dict, encoder: str, adaptive: bool, keep: bool,
                  recycle: bool, workers: int, dry_run: bool,
                  output_dir=None) -> None:
    """开跑之前把这次的关键设置摆在控制台上，省得回头猜是按什么档压的。"""
    enc_cfg = cfg["encode"][encoder]
    bits = [f"编码器 {em(encoder)}（{enc_cfg['codec']} "
            f"preset {enc_cfg['preset']}）"]
    if adaptive and bool(cfg["adaptive"].get("enabled", True)):
        bits.append(f"自适应 CRF，目标 VMAF {em(cfg['adaptive']['target_vmaf'])}")
    else:
        bits.append(f"固定 CRF {em(encode.fmt_crf(enc_cfg['crf']))}")
    bits.append("保留原文件" if keep else em("压缩后删除原文件"))
    if not keep and recycle:
        bits.append("删除走回收站")
    if output_dir is not None:
        bits.append(f"输出到 {em(str(output_dir))}")
    if workers > 1:
        bits.append(f"并发 {workers}")
    if dry_run:
        bits.append(em("预览模式，不改任何文件"))
    log.info("本次设置：" + "，".join(bits))


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    started = time.monotonic()

    try:
        cfg, cfg_path, cfg_dir = _load_config(args.config)
    except config.ConfigError as e:
        print(f"错误: {e}", file=sys.stderr)
        return EXIT_USAGE

    run_cfg = cfg["run"]
    setup_logging(config.resolve_run_path(cfg_dir, run_cfg["log_file"]),
                  run_cfg["log_max_bytes"], run_cfg["log_backups"],
                  argv=["vcompress"] + list(argv if argv is not None
                                            else sys.argv[1:]))
    if cfg_path.exists():
        log.debug(f"配置文件：{cfg_path}")
    else:
        log.info(f"{cfg_path} 不存在，使用内置默认配置"
                 f"（日志与记录写到 {cfg_dir} 下）")
    proc.clear_cancel()

    encoder = args.encoder or cfg["encode"]["encoder"]
    names = config.encoder_names(cfg)
    if encoder not in names:
        log.error(f"没有名为 {encoder!r} 的编码器，配置里定义的是："
                  f"{', '.join(names)}")
        return EXIT_USAGE
    cfg["encode"]["encoder"] = encoder

    output_dir = _check_output_dir(args.output)
    if args.output and output_dir is None:
        return EXIT_USAGE

    try:
        tools = probe.Tools(cfg["tools"])
    except FileNotFoundError as e:
        log.error(str(e))
        return EXIT_USAGE
    log.debug(f"ffmpeg：{tools.ffmpeg}")

    caps = Caps.probe(tools.ffmpeg)
    if not preflight(caps, cfg, encoder, args.adaptive):
        return EXIT_FAILED

    found, invalid = files.gather_files(args.paths, args.recursive,
                                        set(cfg["skip"]["extensions"]))
    if not found:
        if len(invalid) == len(args.paths):
            # 一个有效路径都没有：多半是路径写错了，按用法错误退出
            log.error("没有找到可处理的视频文件：传入的路径全部无效"
                      "（见上面的警告）")
            return EXIT_USAGE
        log.info("没有找到可处理的视频文件")
        return EXIT_OK
    log.info(f"待检查 {em(len(found))} 个文件")

    workers = max(1, args.workers or int(run_cfg["workers"]))
    if args.keep:
        keep = True
    elif args.delete_original:
        keep = False
    else:
        keep = not bool(run_cfg["delete_original"])
    recycle = args.recycle or bool(run_cfg["use_recycle_bin"])
    _log_settings(cfg, encoder, args.adaptive, keep, recycle, workers,
                  args.dry_run, output_dir)

    store = ResultStore(config.resolve_run_path(cfg_dir, run_cfg["results_file"]))
    todo = _pick_todo(found, store, args.force)
    if len(todo) != len(found):
        log.info(f"待处理 {em(len(todo))} 个文件")

    # 只有真正串行执行时进度条才不会被别的文件的输出打断
    serial = workers <= 1 or len(todo) <= 1

    ctx = RunContext(
        cfg=cfg,
        tools=tools,
        caps=caps,
        store=store,
        outputs=files.OutputRegistry(),
        stats=RunStats(),
        encoder=encoder,
        dry_run=args.dry_run,
        adaptive=args.adaptive,
        recycle=recycle,
        keep=keep,
        show_progress=serial and not args.dry_run,
        output_dir=output_dir,
    )

    if not ctx.dry_run:
        files.clean_stale_tmp(todo, ctx.out_ext)
        power.announce(cfg_path, bool(run_cfg["shutdown_when_done"]),
                       int(run_cfg["shutdown_delay_seconds"]))
    else:
        log.debug("预览模式不考虑自动关机")

    try:
        if serial:
            _run_serial(ctx, todo)
        else:
            _run_parallel(ctx, todo, workers)
    except KeyboardInterrupt:
        proc.terminate_all()
        log.warning("已中断：半成品临时文件已清理，原文件未受影响")
        _report(ctx.stats, store, time.monotonic() - started)
        log.info("因为是中断退出，不执行自动关机")
        return EXIT_INTERRUPTED

    _report(ctx.stats, store, time.monotonic() - started)
    if not ctx.dry_run:
        power.maybe_shutdown(cfg_path, int(run_cfg["shutdown_delay_seconds"]))
    return EXIT_FAILED if ctx.stats.counts["failed"] else EXIT_OK
