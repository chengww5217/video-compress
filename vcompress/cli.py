import argparse
import json
import logging
import os
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from . import encode, probe, verify

log = logging.getLogger("vcompress")


class ResultStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.history = {}
        if path.exists():
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        self.history[rec.get("path", "")] = rec
                    except json.JSONDecodeError:
                        continue

    def last(self, path: Path):
        return self.history.get(str(path.resolve()))

    def add(self, action: str, info=None, **extra):
        rec = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "action": action,
            "path": str(info.path.resolve()) if info else None,
        }
        if info is not None:
            rec.update({
                "codec": info.video_codec,
                "size": info.size,
                "duration": round(info.duration, 3),
            })
        rec.update(extra)
        with self.lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if rec["path"]:
                self.history[rec["path"]] = rec


class Ctx:
    pass


def gather_files(paths, recursive: bool, extensions: set):
    files = []
    for p in paths:
        p = Path(p)
        if p.is_file():
            if p.suffix.lower() in extensions and ".tmp." not in p.name:
                files.append(p)
        elif p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            for f in it:
                if f.is_file() and f.suffix.lower() in extensions and ".tmp." not in f.name:
                    files.append(f)
    seen = set()
    unique = []
    for f in files:
        r = str(f.resolve())
        if r not in seen:
            seen.add(r)
            unique.append(f)
    return sorted(unique)


def clean_stale_tmp(files, tmp_suffix: str):
    parents = {f.parent for f in files}
    removed = 0
    for d in parents:
        for t in d.glob(f"*{tmp_suffix}"):
            try:
                t.unlink()
                removed += 1
                log.warning(f"removed stale temp file: {t.name}")
            except OSError as e:
                log.error(f"cannot remove stale temp {t}: {e}")
    return removed


def _fmt_hms(seconds: int) -> str:
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def process_file(ctx: Ctx, src: Path):
    store = ctx.store
    show = ctx.show_progress

    def progress(pct, info_val=0.0, speed=0.0):
        if not show:
            return
        name = src.name[:36]
        if pct is None:
            text = f"\r  {_fmt_hms(int(info_val))} processed   {name:<36}"
        else:
            spd = f"x{speed:.1f}" if speed > 0 else "        "
            eta = f"eta {_fmt_hms(int(info_val)):>7}" if info_val else "eta       --"
            text = f"\r  {pct:5.1f}%  {eta}  {spd:>7}  {name:<36}"
        sys.stdout.write(text)
        sys.stdout.flush()

    try:
        info = probe.probe(ctx.tools, src)
    except Exception as e:
        log.error(f"[fail] {src.name}: probe error: {e}")
        store.add("failed", None, path=str(src.resolve()), stage="probe", error=str(e))
        return "failed"

    skip, reason = probe.should_skip(info, ctx.cfg)
    if skip:
        log.info(f"[skip] {src.name}: {reason}")
        store.add("skipped", info, reason=reason)
        return "skipped"

    enc_cfg = ctx.cfg["encode"][ctx.encoder]
    default_crf = int(enc_cfg["crf"])
    out_ext = ctx.cfg["encode"]["output_container"]
    target = src.with_suffix(out_ext)
    tmp = src.with_name(src.stem + ".tmp" + out_ext)

    if ctx.dry_run:
        mb = (info.size or 0) / 1048576
        log.info(f"[dry-run] would encode {src.name} "
                 f"({info.codec_family or info.video_codec}, {mb:.0f} MB)")
        store.add("dryrun", info)
        return "dryrun"

    try:
        crf = default_crf
        if ctx.adaptive and ctx.cfg["adaptive"].get("enabled", True) and info.duration > 0:
            picked = encode.pick_crf_adaptive(
                ctx.tools, src, info.duration, enc_cfg,
                ctx.cfg["adaptive"], log,
            )
            if picked is not None:
                crf = picked

        cmd = encode.build_cmd(
            ctx.tools.ffmpeg, src, tmp, enc_cfg, crf,
            encode.audio_args(info, ctx.cfg),
            ctx.cfg["skip"]["tag_marker"] + "-v1",
        )
        if show:
            print(f"\n{src.name}  ->  crf={crf} ({ctx.encoder})")
            progress(0.0)
        t0 = time.monotonic()
        encode.run_encode(cmd, info.duration, progress)
        elapsed = time.monotonic() - t0
        if show:
            progress(100.0)
            print()

        out_info = probe.probe(ctx.tools, tmp)

        if ctx.cfg["verify"].get("decode_check", True):
            errs = verify.decode_errors(ctx.tools.ffmpeg, tmp)
            if errs:
                raise RuntimeError("decode check failed: " + "; ".join(errs[:3]))

        ok, msg = verify.check_basic(info, out_info, info.size, tmp.stat().st_size, ctx.cfg)
        if not ok:
            raise RuntimeError(msg)

        score, msg = verify.vmaf_spot_check(ctx.tools, src, tmp, info.duration, ctx.cfg)
        if msg and score is None:
            log.warning(f"{src.name}: vmaf check unavailable ({msg}); proceeding")
        elif msg:
            raise RuntimeError(msg)

        recycle = ctx.recycle
        if target.exists():
            if recycle:
                _trash(target)
            else:
                target.unlink()
        os.replace(tmp, target)
        if str(target.resolve()) != str(src.resolve()):
            if recycle:
                _trash(src)
            else:
                src.unlink()

        saving = 1 - target.stat().st_size / info.size
        log.info(
            f"[ok] {src.name}: crf={crf} saved {saving:.1%} "
            f"({info.size / 1048576:.0f} -> {target.stat().st_size / 1048576:.0f} MB, "
            f"{elapsed:.0f}s)"
        )
        store.add("ok", info, output=str(target), crf=crf,
                  new_size=target.stat().st_size,
                  saving=round(saving, 4), seconds=round(elapsed, 1))
        return "ok"

    except Exception as e:
        log.error(f"[fail] {src.name}: {e}")
        store.add("failed", info, stage="process", error=str(e)[:500])
        if tmp.exists():
            try:
                tmp.unlink()
                log.info(f"cleaned temp file: {tmp.name}")
            except OSError:
                pass
        return "failed"


def _trash(path: Path):
    try:
        from send2trash import send2trash
        send2trash(str(path))
    except ImportError:
        log.warning("send2trash not installed; permanently deleting instead")
        path.unlink()


def setup_logging(log_file: str):
    lf = Path(log_file)
    lf.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    fh = logging.FileHandler(lf, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(ch)
    root.addHandler(fh)


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"error: config file not found: {path}\n"
                 f"pass --config or restore config.toml next to the package")
    with open(path, "rb") as f:
        return tomllib.load(f)


def main():
    ap = argparse.ArgumentParser(
        prog="vcompress",
        description="ffmpeg-based visually-lossless video compressor",
    )
    ap.add_argument("paths", nargs="+", help="video files or directories")
    ap.add_argument("--config",
                    default=str(Path(__file__).resolve().parents[1] / "config.toml"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="re-process files skipped/ok in previous runs")
    ap.add_argument("--no-recursive", dest="recursive", action="store_false")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--encoder", choices=["av1", "x265"], default=None)
    ap.add_argument("--no-adaptive", dest="adaptive", action="store_false")
    ap.add_argument("--recycle", action="store_true",
                    help="move originals to recycle bin instead of deleting")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    run_cfg = cfg["run"]
    setup_logging(run_cfg["log_file"])
    if args.encoder:
        cfg["encode"]["encoder"] = args.encoder
    encoder = cfg["encode"]["encoder"]

    tools = probe.Tools(cfg["tools"])
    log.info(f"ffmpeg: {tools.ffmpeg}")

    exts = {e.lower() for e in cfg["skip"]["extensions"]}
    files = gather_files(args.paths, args.recursive, exts)
    if not files:
        log.info("no video files found")
        return 0
    log.info(f"{len(files)} file(s) to examine")

    store_path = Path(run_cfg["results_file"])
    store = ResultStore(store_path)

    ctx = Ctx()
    ctx.cfg = cfg
    ctx.tools = tools
    ctx.store = store
    ctx.dry_run = args.dry_run
    ctx.adaptive = args.adaptive
    ctx.encoder = encoder
    ctx.recycle = args.recycle or run_cfg.get("use_recycle_bin", False)
    workers = args.workers or int(run_cfg.get("workers", 1))
    ctx.show_progress = workers == 1 and not args.dry_run

    todo = []
    for f in files:
        last = store.last(f)
        if last and last["action"] in ("ok", "skipped") and not args.force:
            log.info(f"[cached] {f.name}: {last['action']} previously, skipping "
                     f"(use --force to override)")
            continue
        todo.append(f)

    if not ctx.dry_run:
        clean_stale_tmp(todo, ".tmp" + cfg["encode"]["output_container"])

    counts = {"ok": 0, "skipped": 0, "failed": 0, "dryrun": 0}
    try:
        if workers <= 1 or len(todo) == 1:
            for f in todo:
                counts[process_file(ctx, f)] += 1
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(process_file, ctx, f): f for f in todo}
                for fut in as_completed(futures):
                    counts[fut.result()] += 1
    except KeyboardInterrupt:
        encode.terminate_all()
        log.warning("interrupted; temp files cleaned up on next run")
        return 130

    total_saved = 0
    for rec in store.history.values():
        if rec.get("action") == "ok" and rec.get("size") and rec.get("new_size"):
            total_saved += max(0, rec["size"] - rec["new_size"])
    log.info(
        f"done: {counts['ok']} compressed, {counts['skipped']} skipped, "
        f"{counts['failed']} failed"
        + (f", {counts['dryrun']} dry-run" if counts["dryrun"] else "")
    )
    if counts["ok"]:
        log.info(f"session savings: see {store_path}")
    return 1 if counts["failed"] else 0
