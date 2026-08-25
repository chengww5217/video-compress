"""全部任务结束后的自动关机。

三个刻意的设计：

* 开关只认**配置文件**里的 `[run] shutdown_when_done`，命令行没有对应参数。
* 所有任务跑完之后才重新读一遍配置文件。压几个小时的过程中改主意（想关 /
  不想关）都来得及，不用重启整个任务。
* 真要关机之前先把配置写回 `false`。这个开关是一次性的：下次想关机必须再手动
  打开，绝不会因为忘了改配置而莫名其妙关机。改不动配置就放弃关机——宁可不关，
  也不能留下一个每次运行都关机的隐患。
"""

import logging
import platform
import re
import shutil
import time
import tomllib
from pathlib import Path

log = logging.getLogger("vcompress")

CONFIG_KEY = "shutdown_when_done"

_FLAG_LINE = re.compile(
    rf"^(\s*{CONFIG_KEY}\s*=\s*)(true|false)(\s*(?:#.*)?)$")
_SECTION = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")


def announce(cfg_path: Path, enabled: bool, delay: int) -> None:
    """任务开始时把关机设置打到控制台，顺带告诉用户怎么改。"""
    if not cfg_path.exists():
        log.info(f"auto shutdown: OFF (no config file at {cfg_path}; "
                 f"create one with [run] {CONFIG_KEY} = true to power off "
                 f"when everything is done)")
        return
    if enabled:
        log.info(f"auto shutdown: ON — the machine powers off {delay}s after "
                 f"all tasks finish; the flag is read again at the end, so "
                 f"setting [run] {CONFIG_KEY} = false in {cfg_path} while the "
                 f"run is in progress still cancels it")
    else:
        log.info(f"auto shutdown: OFF — set [run] {CONFIG_KEY} = true in "
                 f"{cfg_path} to power off {delay}s after all tasks finish; "
                 f"you can flip it while the run is in progress (it is read "
                 f"again at the end, and reset to false before shutting down)")


def read_flag(cfg_path: Path) -> bool:
    """从磁盘重新读一次开关。

    只挑这一个键，不做全量校验：跑到这一步编码都已经完事了，为了配置文件里
    某个无关的笔误而崩掉没有意义。文件坏了/读不了就当没开。
    """
    if not cfg_path.exists():
        log.debug(f"no config file at {cfg_path}; auto shutdown stays off")
        return False
    try:
        with open(cfg_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning(f"cannot re-read {cfg_path} ({e}); skipping auto shutdown")
        return False
    run = data.get("run")
    value = run.get(CONFIG_KEY) if isinstance(run, dict) else None
    if value is None:
        log.debug(f"[run] {CONFIG_KEY} is not set in {cfg_path}; "
                  f"auto shutdown stays off")
        return False
    if not isinstance(value, bool):
        log.warning(f"[run] {CONFIG_KEY} in {cfg_path} is {value!r}, not a "
                    f"boolean; skipping auto shutdown")
        return False
    log.info(f"re-read {cfg_path}: [run] {CONFIG_KEY} = "
             f"{'true' if value else 'false'}")
    return value


def disarm(cfg_path: Path) -> bool:
    """把配置里的开关改回 false，成功返回 True。

    只改那一行的取值，注释和其它内容原样保留；先写临时文件再原子替换，写坏了
    磁盘上仍是原来那份。
    """
    try:
        text = cfg_path.read_text(encoding="utf-8")
    except OSError as e:
        log.error(f"cannot read {cfg_path} to reset [run] {CONFIG_KEY} ({e})")
        return False

    lines = text.splitlines(keepends=True)
    section = None
    hit = None
    for i, line in enumerate(lines):
        m = _SECTION.match(line)
        if m:
            section = m.group(1).strip()
            continue
        if section != "run":
            continue
        m = _FLAG_LINE.match(line.rstrip("\r\n"))
        if m:
            newline = line[len(line.rstrip("\r\n")):]
            lines[i] = f"{m.group(1)}false{m.group(3)}{newline}"
            hit = i
    if hit is None:
        log.error(f"cannot find [run] {CONFIG_KEY} in {cfg_path}; "
                  f"skipping auto shutdown so it cannot repeat")
        return False

    tmp = cfg_path.with_name(cfg_path.name + ".tmp")
    try:
        tmp.write_text("".join(lines), encoding="utf-8")
        tmp.replace(cfg_path)
    except OSError as e:
        log.error(f"cannot write {cfg_path} to reset [run] {CONFIG_KEY} ({e})")
        tmp.unlink(missing_ok=True)
        return False
    log.info(f"reset [run] {CONFIG_KEY} = false in {cfg_path}; the next run "
             f"will not power off unless you turn it on again")
    return True


def shutdown_command() -> list | None:
    """本平台的立即关机命令；不认识的平台返回 None。"""
    system = platform.system()
    if system == "Windows":
        return ["shutdown", "/s", "/t", "0"]
    if system == "Darwin":
        # osascript 走 System Events，普通用户就能关（sudo shutdown 要密码）
        return ["osascript", "-e",
                'tell application "System Events" to shut down']
    if system == "Linux":
        if shutil.which("systemctl"):
            return ["systemctl", "poweroff"]
        return ["shutdown", "-h", "now"]
    log.error(f"no shutdown command known for {system!r}")
    return None


def _countdown(delay: int) -> bool:
    """倒计时；被 Ctrl+C 打断返回 False（取消关机）。"""
    if delay <= 0:
        return True
    log.info(f"shutting down in {delay}s — press Ctrl+C to cancel")
    left = delay
    try:
        while left > 0:
            step = min(10, left)
            time.sleep(step)
            left -= step
            if left:
                log.info(f"shutting down in {left}s")
    except KeyboardInterrupt:
        log.warning("auto shutdown cancelled")
        return False
    return True


def maybe_shutdown(cfg_path: Path, delay: int, runner=None) -> bool:
    """任务全部结束后按配置文件决定是否关机，返回是否真的下发了关机命令。

    顺序是固定的：重读配置 -> 把配置改回 false -> 倒计时 -> 下发命令。改配置
    失败就不关机。
    """
    if not read_flag(cfg_path):
        log.info("auto shutdown is off; leaving the machine running")
        return False
    if not disarm(cfg_path):
        return False
    cmd = shutdown_command()
    if not cmd:
        return False
    if not _countdown(int(delay)):
        return False

    from . import proc
    log.info("running: " + " ".join(cmd))
    run = runner or proc.run_managed
    try:
        code, out, err = run(cmd)
    except OSError as e:
        log.error(f"shutdown command failed to start: {e}")
        return False
    if code != 0:
        detail = (err or out or "").strip()
        log.error(f"shutdown command exited with {code}"
                  + (f": {detail}" if detail else ""))
        return False
    log.info("shutdown command accepted; goodbye")
    return True
