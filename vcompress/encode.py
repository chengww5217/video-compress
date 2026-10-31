"""编码参数生成与编码执行。

只管一件事：把「用哪个编码器、什么 CRF、哪些轨道」变成一条 ffmpeg 命令，然后
把它跑起来并解析进度。构建能力与像素格式在 caps.py，音轨字幕在 tracks.py，
画质测量在 vmaf.py，CRF 怎么选在 adaptive.py。
"""

import collections
import shlex
import threading
import time
from pathlib import Path

from . import proc
from .tracks import MKV_LIKE, MP4_LIKE

STDERR_TAIL_LINES = 200


class EncodeError(RuntimeError):
    pass


# ------------------------------------------------------------------------- CRF

# CRF 的实际可用粒度。SVT-AV1 的 --crf 接受 0.25 的增量，x265 接受任意小数；
# 统一按 0.25 对齐，比这更细对 AV1 没有意义（编码器自己会对齐回来）。
# 为什么要小数：本机实测 1 级 CRF ≈ 8% 体积，0.25 级 ≈ 2%。只搜整数等于平均
# 白扔半级 ≈ 4% 的体积，而画质闸门一点没变——纯亏。
CRF_GRID = 0.25


def quantize_crf(crf: float) -> float:
    """把 CRF 对齐到 0.25 的网格，并去掉 35.0 这种多余的小数。"""
    value = round(float(crf) / CRF_GRID) * CRF_GRID
    return int(value) if float(value).is_integer() else round(value, 2)


def fmt_crf(crf) -> str:
    return str(int(crf)) if float(crf).is_integer() else f"{float(crf):g}"


def merge_dict_param(extra: str, option: str, key: str, value: str) -> str:
    """往 `-svtav1-params` / `-x265-params` 这类字典型选项里塞一个键。

    不能简单在命令行上再加一个同名选项：第二个会把第一个整个顶掉，
    `extra` 里那一串调优参数就全丢了。同键的旧值被替换，其余参数原样保留。
    """
    tokens = shlex.split(extra or "")
    setting = f"{key}={value}"
    if option in tokens:
        i = tokens.index(option) + 1
        if i < len(tokens):
            kept = [p for p in tokens[i].split(":")
                    if p and not p.startswith(f"{key}=")]
            tokens[i] = ":".join(kept + [setting])
            return shlex.join(tokens)
    return shlex.join(tokens + [option, setting])


def with_svtav1_param(extra: str, key: str, value: str) -> str:
    """往 -svtav1-params 里塞一个键，没有这个开关就补一个。"""
    return merge_dict_param(extra, "-svtav1-params", key, value)


def crf_args(encoder_cfg: dict, crf) -> tuple:
    """返回 (命令行参数, 改写后的 extra)。

    小数 CRF 各家的下发方式不同：libx265 的 `-crf` 本身就是浮点，libsvtav1 的
    `-crf` 是整数、只能走 `-svtav1-params crf=`（实测 params 会覆盖 -crf，所以
    整数那个仍然留着，给忽略这个参数的老构建兜底）。不认识的编码器取整，宁可
    少省一点也不要编出一个 CRF 完全不对的文件。
    """
    codec = encoder_cfg["codec"]
    extra = encoder_cfg.get("extra", "")
    crf = quantize_crf(crf)
    if float(crf).is_integer():
        return ["-crf", str(int(crf))], extra
    if codec == "libsvtav1":
        return (["-crf", str(int(crf))],
                with_svtav1_param(extra, "crf", fmt_crf(crf)))
    if codec == "libx265":
        return ["-crf", f"{float(crf):.2f}"], extra
    return ["-crf", str(int(round(crf)))], extra


# ------------------------------------------------------------------ 命令行拼装

def video_args(encoder_cfg: dict, crf) -> list:
    crf_flags, extra = crf_args(encoder_cfg, crf)
    args = [
        "-c:v", encoder_cfg["codec"],
        "-preset", str(encoder_cfg["preset"]),
        *crf_flags,
    ]
    pix_fmt = encoder_cfg.get("pix_fmt")
    if pix_fmt:
        args += ["-pix_fmt", pix_fmt]
    if extra:
        args.extend(shlex.split(extra))
    return args


def merge_comment(original: str, marker: str) -> str:
    """把标记追加到原有 comment 后面，而不是把人家的注释盖掉。

    `-map_metadata 0` 会把原始 comment 带过来，紧接着的 `-metadata comment=`
    是整条覆盖，所以要自己拼。已经含标记就不重复追加。
    """
    original = (original or "").strip()
    if not original:
        return marker
    if marker.lower() in original.lower():
        return original
    return f"{original} | {marker}"


def color_args(info) -> list:
    """把源的色彩属性显式写给编码器。

    新一点的 ffmpeg 会自动把这些属性从解码器传到编码器（本机 ffmpeg 9 实测
    四项和 HDR10 母版信息都能自动带过去），但这依赖构建版本，而丢了它们的后果
    是 HDR 片源被当成 SDR 播——画面数值一模一样，VMAF 测不出来。显式写一遍是
    零成本的保险；源没标的项不写，免得凭空造出错误的标记。
    """
    args = []
    for attr, flag in (("color_primaries", "-color_primaries"),
                       ("color_transfer", "-color_trc"),
                       ("color_space", "-colorspace"),
                       ("color_range", "-color_range")):
        value = getattr(info, attr, "") or ""
        if value:
            args += [flag, value]
    return args


def container_maps_and_tail(container: str) -> tuple:
    """按目标容器决定流映射与收尾参数，返回 (maps, tail)。"""
    maps = ["-map", "0:V", "-map", "0:a?", "-map", "0:s?"]
    tail = []
    if container in MKV_LIKE:
        # 附件只有 matroska 支持；data 流（如 mp4 的 tmcd 时间码）装不进去，
        # 硬塞会让整个转码失败，只能显式丢弃。
        maps += ["-map", "0:t?"]
        tail += ["-c:t", "copy", "-dn"]
    else:
        maps += ["-map", "0:d?"]
        tail += ["-c:d", "copy"]
    if container in MP4_LIKE:
        tail += ["-movflags", "+use_metadata_tags"]
    return maps, tail


def build_cmd(ffmpeg: str, src: Path, dst: Path, encoder_cfg: dict,
              crf: float, a_args: list, s_args: list, marker: str,
              original_comment: str = "", c_args: list = None) -> list:
    container = dst.suffix.lower()
    maps, tail = container_maps_and_tail(container)
    return [
        ffmpeg, "-hide_banner", "-y",
        "-i", str(src),
        *maps,
        "-map_metadata", "0",
        "-map_chapters", "0",
        *video_args(encoder_cfg, crf),
        *(c_args or []),
        *a_args,
        *s_args,
        *tail,
        "-metadata", f"comment={merge_comment(original_comment, marker)}",
        "-progress", "pipe:1", "-nostats",
        str(dst),
    ]


# ------------------------------------------------------------------ 编码与进度

# ffmpeg 每刷新一次进度会同时发 out_time_us、out_time_ms、out_time 三个键，
# 认多个键就会让同一个时刻回调多次（进度条重画、速度平滑被污染）。
TIME_KEYS = ("out_time_us", "out_time_ms")


def _parse_progress_line(line: str):
    """解析 -progress 的一行，返回 (键名, 秒) 或 None。

    out_time_ms 名字骗人：ffmpeg 往里写的其实也是微秒。
    """
    if "=" not in line:
        return None
    key, _, value = line.partition("=")
    if key not in TIME_KEYS:
        return None
    try:
        return key, float(value.strip()) / 1e6
    except ValueError:
        return None


def _parse_speed(line: str):
    if not line.startswith("speed="):
        return None
    value = line.partition("=")[2].strip().rstrip("x")
    try:
        return float(value)
    except ValueError:
        return None


def _drain(stream, sink) -> None:
    """把子进程 stderr 持续读走，防止管道写满后双向阻塞。"""
    try:
        for line in stream:
            sink.append(line)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def run_encode(cmd: list, total_duration: float, progress_cb=None) -> None:
    process = proc.spawn(cmd)
    # ffmpeg 的进度走 stdout，日志走 stderr；stderr 必须并行读，否则
    # 告警较多的片源写满 64KB 管道后 ffmpeg 和本进程互相等死。
    err_tail = collections.deque(maxlen=STDERR_TAIL_LINES)
    err_thread = threading.Thread(target=_drain, args=(process.stderr, err_tail),
                                  daemon=True)
    err_thread.start()
    start = time.monotonic()
    last_time = start
    last_pos = 0.0
    last_speed = 0.0
    time_key = None
    try:
        for raw in process.stdout:
            parsed = _parse_progress_line(raw.strip())
            if parsed is None:
                continue
            key, pos = parsed
            # 锁定第一个见到的时间键，后面只认它，一次刷新只回调一次
            if time_key is None:
                time_key = key
            elif key != time_key:
                continue
            now = time.monotonic()
            if pos > last_pos and now > last_time:
                inst = (pos - last_pos) / (now - last_time)
                last_speed = 0.5 * last_speed + 0.5 * inst
            last_time = now
            last_pos = pos
            if progress_cb:
                if total_duration > 0:
                    pct = min(99.9, pos / total_duration * 100)
                    remaining = max(0.0, total_duration - pos)
                    eta = remaining / last_speed if last_speed > 0 else 0.0
                    progress_cb(pct, eta, last_speed)
                else:
                    progress_cb(None, pos, last_speed)
        rc = process.wait()
    except BaseException:
        try:
            process.kill()
        except OSError:
            pass
        raise
    finally:
        proc.unregister(process)
        err_thread.join(timeout=5)
        try:
            process.stdout.close()
        except OSError:
            pass
    if rc != 0:
        tail = "".join(err_tail).strip()[-500:]
        raise EncodeError(f"ffmpeg 退出码 {rc}：{tail}")
