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
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
    """只清理待处理文件自己留下的临时文件，不碰目录里同后缀的其它文件。"""
    removed = 0
    for f in files:
        t = f.with_name(f.stem + tmp_suffix)
        if not t.exists():
            continue
        try:
            t.unlink()
            removed += 1
            log.warning(f"removed stale temp file: {t.name}")
        except OSError as e:
            log.error(f"cannot remove stale temp {t}: {e}")
    return removed


def _fmt_hms(seconds: int) -> str:
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.samefile(b)
    except OSError:
        return str(a.resolve()) == str(b.resolve())


def resolve_final_path(src: Path, target: Path, out_ext: str, encoder: str,
                       keep: bool, log=None) -> Path:
    """决定最终输出路径，任何情况下都不覆盖已存在的其它文件。

    非 keep 模式下 target 正好是原文件时属于预期内的原地替换；其余情况只要
    target 已被占用，就退让到 <名字>.<编码器><扩展名>，该名字也被占用则失败。
    """
    if not target.exists():
        return target
    if not keep and _same_path(target, src):
        return target
    alt = src.with_name(f"{src.stem}.{encoder}{out_ext}")
    if alt.exists():
        raise RuntimeError(
            f"output already exists, refusing to overwrite: {target.name} "
            f"(fallback {alt.name} is taken too)"
        )
    if log:
        log.warning(f"{target.name} already exists and is not the source; "
                    f"writing to {alt.name} instead")
    return alt


def process_file(ctx: Ctx, src: Path):
    store = ctx.store
    show = ctx.show_progress
    line_open = [False]

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
        line_open[0] = True

    def end_progress():
        """收尾进度行，避免后面的日志直接粘在 \\r 行尾。"""
        if line_open[0]:
            sys.stdout.write("\n")
            sys.stdout.flush()
            line_open[0] = False

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
        # 先判定输出名，避免编码几小时后才发现名字冲突
        resolve_final_path(src, target, out_ext, ctx.encoder, ctx.keep)
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
            encode.subtitle_args(info, out_ext),
            ctx.cfg["skip"]["tag_marker"] + "-v1",
        )
        log.info(f"[encode] {src.name}: crf={crf} ({ctx.encoder})")
        progress(0.0)
        t0 = time.monotonic()
        encode.run_encode(cmd, info.duration, progress)
        elapsed = time.monotonic() - t0
        progress(100.0)
        end_progress()

        out_info = probe.probe(ctx.tools, tmp)

        if ctx.cfg["verify"].get("decode_check", True):
            errs = verify.decode_errors(ctx.tools.ffmpeg, tmp)
            if errs:
                raise RuntimeError("decode check failed: " + "; ".join(errs[:3]))

        ok, msg = verify.check_basic(info, out_info, info.size, tmp.stat().st_size, ctx.cfg)
        if not ok:
            raise RuntimeError(msg)

        score, msg = verify.vmaf_spot_check(ctx.tools, src, tmp,
                                            info.duration, ctx.cfg, log=log,
                                            fps=info.fps)
        if score is not None:
            log.info(f"[verify] {src.name}: vmaf {score:.2f}")
        if msg and score is None:
            log.warning(f"{src.name}: vmaf check unavailable ({msg}); proceeding")
        elif msg:
            raise RuntimeError(msg)

        recycle = ctx.recycle
        final = resolve_final_path(src, target, out_ext, ctx.encoder,
                                   ctx.keep, log)
        in_place = not ctx.keep and _same_path(final, src)
        if in_place and recycle:
            _trash(src)
        os.replace(tmp, final)
        if not ctx.keep and not in_place:
            if recycle:
                _trash(src)
            else:
                src.unlink()

        saving = 1 - final.stat().st_size / info.size
        kept = ", original kept" if ctx.keep else ""
        log.info(
            f"[ok] {src.name}: crf={crf} saved {saving:.1%} "
            f"({info.size / 1048576:.0f} -> {final.stat().st_size / 1048576:.0f} MB, "
            f"{elapsed:.0f}s{kept})"
        )
        store.add("ok", info, output=str(final), crf=crf,
                  new_size=final.stat().st_size,
                  saving=round(saving, 4), seconds=round(elapsed, 1),
                  kept=ctx.keep)
        return "ok"

    except Exception as e:
        end_progress()
        if encode.is_cancelled():
            log.info(f"[abort] {src.name}: interrupted")
            _remove_tmp(tmp)
            return "aborted"
        log.error(f"[fail] {src.name}: {e}")
        kept_tmp = None
        if ctx.cfg["verify"].get("keep_failed_output", False):
            kept_tmp = _keep_failed_tmp(tmp, src, out_ext)
        store.add("failed", info, stage="process", error=str(e)[:500],
                  failed_output=str(kept_tmp) if kept_tmp else None)
        if kept_tmp is None:
            _remove_tmp(tmp)
        return "failed"


def _keep_failed_tmp(tmp: Path, src: Path, out_ext: str):
    """校验没过时按需留下产物，别让几小时的编码结果直接消失。

    命名 <名字>.failed<扩展名>，名字被占就不留（本项目一律不覆盖已有文件）。
    """
    if not tmp.exists():
        return None
    dest = src.with_name(f"{src.stem}.failed{out_ext}")
    if dest.exists():
        log.warning(f"{dest.name} already exists; discarding {tmp.name} instead")
        return None
    try:
        os.replace(tmp, dest)
    except OSError as e:
        log.error(f"cannot keep failed output {dest.name}: {e}")
        return None
    log.warning(f"kept the rejected output for inspection: {dest.name} "
                f"(original untouched)")
    return dest


def _remove_tmp(tmp: Path):
    if tmp.exists():
        try:
            tmp.unlink()
            log.info(f"cleaned temp file: {tmp.name}")
        except OSError:
            pass


def _trash(path: Path):
    try:
        from send2trash import send2trash
        send2trash(str(path))
    except ImportError:
        log.warning("send2trash not installed; permanently deleting instead")
        path.unlink()


def preflight(tools, cfg: dict, encoder: str, adaptive: bool) -> bool:
    """先确认这个 ffmpeg 构建能干活，别等编码到一半才报错。"""
    codec = cfg["encode"][encoder]["codec"]
    encoders = encode.available_encoders(tools.ffmpeg)
    if not encoders:
        log.warning("cannot list ffmpeg encoders; skipping the build self-check")
        return True
    if codec not in encoders:
        log.error(f"this ffmpeg build has no {codec} (needed for --encoder "
                  f"{encoder}); install a full build or switch encoders")
        return False
    if "libopus" not in encoders:
        log.warning("this ffmpeg build has no libopus; lossless or very "
                    "high bitrate audio tracks cannot be transcoded")

    wants_vmaf = (adaptive and cfg["adaptive"].get("enabled", True)) or \
        cfg["verify"].get("vmaf_enabled", True)
    if wants_vmaf:
        missing = []
        if "libvmaf" not in encode.available_filters(tools.ffmpeg):
            missing.append("the libvmaf filter")
        if "libx264" not in encoders:
            missing.append("libx264 (used for the lossless sample segments)")
        if missing:
            log.warning("this ffmpeg build lacks " + " and ".join(missing) +
                        "; adaptive CRF and the VMAF spot check will fall back")
    return True


def resolve_run_path(base: Path, value: str) -> Path:
    """[run] 里的相对路径按配置文件所在目录解析，而不是当前工作目录。"""
    p = Path(value).expanduser()
    return p if p.is_absolute() else base / p


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
    ap.add_argument("--keep", action="store_true",
                    help="keep the original file; only write the compressed copy "
                         "(collisions are named <name>.<encoder><ext>, still "
                         "colliding files fail instead of being overwritten)")
    args = ap.parse_args()

    cfg_path = Path(args.config).expanduser().resolve()
    cfg = load_config(cfg_path)
    run_cfg = cfg["run"]
    cfg_dir = cfg_path.parent
    setup_logging(resolve_run_path(cfg_dir, run_cfg["log_file"]))
    encode.clear_cancel()
    if args.encoder:
        cfg["encode"]["encoder"] = args.encoder
    encoder = cfg["encode"]["encoder"]

    tools = probe.Tools(cfg["tools"])
    log.info(f"ffmpeg: {tools.ffmpeg}")

    if not preflight(tools, cfg, encoder, args.adaptive):
        return 1

    exts = {e.lower() for e in cfg["skip"]["extensions"]}
    files = gather_files(args.paths, args.recursive, exts)
    if not files:
        log.info("no video files found")
        return 0
    log.info(f"{len(files)} file(s) to examine")

    store_path = resolve_run_path(cfg_dir, run_cfg["results_file"])
    store = ResultStore(store_path)

    ctx = Ctx()
    ctx.cfg = cfg
    ctx.tools = tools
    ctx.store = store
    ctx.dry_run = args.dry_run
    ctx.adaptive = args.adaptive
    ctx.encoder = encoder
    ctx.recycle = args.recycle or run_cfg.get("use_recycle_bin", False)
    ctx.keep = args.keep or run_cfg.get("keep_original", False)
    workers = args.workers or int(run_cfg.get("workers", 1))

    todo = []
    for f in files:
        last = store.last(f)
        if last and last["action"] in ("ok", "skipped") and not args.force:
            log.info(f"[cached] {f.name}: {last['action']} previously, skipping "
                     f"(use --force to override)")
            continue
        todo.append(f)

    # 只有真正串行执行时进度条才不会被别的文件的输出打断
    serial = workers <= 1 or len(todo) == 1
    ctx.show_progress = serial and not ctx.dry_run

    if not ctx.dry_run:
        clean_stale_tmp(todo, ".tmp" + cfg["encode"]["output_container"])

    counts = {"ok": 0, "skipped": 0, "failed": 0, "dryrun": 0, "aborted": 0}
    try:
        if serial:
            for f in todo:
                counts[process_file(ctx, f)] += 1
        else:
            # 不能用 with：KeyboardInterrupt 会先触发 shutdown(wait=True)，
            # 卡在等编码结束上，terminate_all 永远排不到。
            ex = ThreadPoolExecutor(max_workers=workers)
            try:
                futures = {ex.submit(process_file, ctx, f): f for f in todo}
                for fut in as_completed(futures):
                    counts[fut.result()] += 1
            except BaseException:
                encode.terminate_all()
                ex.shutdown(wait=False, cancel_futures=True)
                raise
            ex.shutdown(wait=True)
    except KeyboardInterrupt:
        encode.terminate_all()
        log.warning("interrupted; partial outputs removed")
        return 130

    total_saved = 0
    for rec in store.history.values():
        if rec.get("action") == "ok" and rec.get("size") and rec.get("new_size"):
            total_saved += max(0, rec["size"] - rec["new_size"])
    log.info(
        f"done: {counts['ok']} compressed, {counts['skipped']} skipped, "
        f"{counts['failed']} failed"
        + (f", {counts['dryrun']} dry-run" if counts["dryrun"] else "")
        + (f", {counts['aborted']} aborted" if counts["aborted"] else "")
    )
    if counts["ok"]:
        log.info(f"session savings: see {store_path}")
    return 1 if counts["failed"] else 0
