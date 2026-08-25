"""命令行入口：参数解析、构建自检、调度与汇总。"""

import argparse
import logging
import logging.handlers
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import config, files, pipeline, probe, proc
from .caps import Caps
from .pipeline import RunContext, RunStats
from .results import ResultStore
from .util import fmt_size

log = logging.getLogger("vcompress")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config.toml"


def setup_logging(log_file: Path, max_bytes: int = 5_242_880,
                  backups: int = 3) -> None:
    """控制台 INFO、文件 DEBUG。文件按大小轮转，长期跑不会无限膨胀。"""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max(0, int(max_bytes)),
        backupCount=max(0, int(backups)), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(ch)
    root.addHandler(fh)


def preflight(caps: Caps, cfg: dict, encoder: str,
              adaptive: bool) -> bool:
    """先确认这个 ffmpeg 构建能干活，别等编码到一半才报错。"""
    codec = cfg["encode"][encoder]["codec"]
    if not caps.known:
        log.warning("cannot list ffmpeg encoders; skipping the build self-check")
        return True
    if codec not in caps.encoders:
        log.error(f"this ffmpeg build has no {codec} (needed for --encoder "
                  f"{encoder}); install a full build or switch encoders")
        return False
    if "libopus" not in caps.encoders:
        log.warning("this ffmpeg build has no libopus; lossless or very "
                    "high bitrate audio tracks cannot be transcoded")

    wants_vmaf = (adaptive and cfg["adaptive"].get("enabled", True)) or \
        cfg["verify"].get("vmaf_enabled", True)
    if wants_vmaf:
        if "libvmaf" not in caps.filters:
            log.warning("this ffmpeg build lacks the libvmaf filter; adaptive "
                        "CRF and the VMAF spot check will fall back")
        elif caps.lossless_codec != "libx264":
            log.warning(f"this ffmpeg build has no libx264; sampling with "
                        f"{caps.lossless_codec} instead (bigger temp files)")
    return True


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="vcompress",
        description="ffmpeg-based visually-lossless video compressor",
    )
    ap.add_argument("paths", nargs="+", help="video files or directories")
    ap.add_argument("--config", default=None,
                    help=f"config file (default: {DEFAULT_CONFIG})")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="re-process files skipped/ok in previous runs")
    ap.add_argument("--no-recursive", dest="recursive", action="store_false")
    ap.add_argument("--workers", type=int, default=None)
    # 候选值来自配置里的 [encode.*] 子表，不在这里硬编码
    ap.add_argument("--encoder", default=None,
                    help="override [encode] encoder (must have a matching "
                         "[encode.<name>] table)")
    ap.add_argument("--no-adaptive", dest="adaptive", action="store_false")
    ap.add_argument("--recycle", action="store_true",
                    help="move originals to recycle bin instead of deleting")
    keep = ap.add_mutually_exclusive_group()
    keep.add_argument("--keep", action="store_true",
                      help="keep the original file; only write the compressed copy "
                           "(collisions are named <name>.<encoder><ext>, still "
                           "colliding files fail instead of being overwritten; "
                           "this is the default)")
    keep.add_argument("--delete-original", action="store_true",
                      help="delete the original file once the compressed copy "
                           "passes verification (overrides the default keep)")
    return ap


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
            log.info(f"[cached] {f.name}: {cached['action']} previously, "
                     f"skipping (use --force to override)")
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


def _report(stats: RunStats, store: ResultStore) -> None:
    counts = stats.counts
    parts = [f"{counts['ok']} compressed", f"{counts['skipped']} skipped",
             f"{counts['failed']} failed"]
    if counts["dryrun"]:
        parts.append(f"{counts['dryrun']} dry-run")
    if counts["aborted"]:
        parts.append(f"{counts['aborted']} aborted")
    log.info("done: " + ", ".join(parts))
    if counts["ok"]:
        log.info(f"saved {fmt_size(stats.saved_bytes)} this run, "
                 f"{fmt_size(store.total_saved())} in total "
                 f"({store.path})")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    try:
        cfg, cfg_path, cfg_dir = _load_config(args.config)
    except config.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE

    run_cfg = cfg["run"]
    setup_logging(config.resolve_run_path(cfg_dir, run_cfg["log_file"]),
                  run_cfg["log_max_bytes"], run_cfg["log_backups"])
    if cfg_path.exists():
        log.debug(f"config: {cfg_path}")
    else:
        log.info(f"no config file at {cfg_path}; using built-in defaults "
                 f"(logs and records go under {cfg_dir})")
    proc.clear_cancel()

    encoder = args.encoder or cfg["encode"]["encoder"]
    names = config.encoder_names(cfg)
    if encoder not in names:
        log.error(f"unknown encoder {encoder!r}; the config defines: "
                  f"{', '.join(names)}")
        return EXIT_USAGE
    cfg["encode"]["encoder"] = encoder

    try:
        tools = probe.Tools(cfg["tools"])
    except FileNotFoundError as e:
        log.error(str(e))
        return EXIT_USAGE
    log.info(f"ffmpeg: {tools.ffmpeg}")

    caps = Caps.probe(tools.ffmpeg)
    if not preflight(caps, cfg, encoder, args.adaptive):
        return EXIT_FAILED

    found = files.gather_files(args.paths, args.recursive,
                              set(cfg["skip"]["extensions"]))
    if not found:
        log.info("no video files found")
        return EXIT_OK
    log.info(f"{len(found)} file(s) to examine")

    store = ResultStore(config.resolve_run_path(cfg_dir, run_cfg["results_file"]))
    todo = _pick_todo(found, store, args.force)

    workers = max(1, args.workers or int(run_cfg["workers"]))
    # 只有真正串行执行时进度条才不会被别的文件的输出打断
    serial = workers <= 1 or len(todo) <= 1

    if args.keep:
        keep = True
    elif args.delete_original:
        keep = False
    else:
        keep=not bool(run_cfg["delete_original"]),

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
        recycle=args.recycle or bool(run_cfg["use_recycle_bin"]),
        keep=keep,
        show_progress=serial and not args.dry_run,
    )

    if not ctx.dry_run:
        files.clean_stale_tmp(todo, ctx.out_ext)

    try:
        if serial:
            _run_serial(ctx, todo)
        else:
            _run_parallel(ctx, todo, workers)
    except KeyboardInterrupt:
        proc.terminate_all()
        log.warning("interrupted; partial outputs removed")
        _report(ctx.stats, store)
        return EXIT_INTERRUPTED

    _report(ctx.stats, store)
    return EXIT_FAILED if ctx.stats.counts["failed"] else EXIT_OK
