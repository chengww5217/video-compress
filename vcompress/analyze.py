"""片源探查：扫一遍源文件，决定这个文件该怎么压。

为什么需要这一步：下载来的片库里什么都有——DVD/电视台 rip 是交错的，电影 rip
带死黑边，同一个目录里每个文件都是不同的人用不同参数压的。这些差异不是靠一套
全局配置能覆盖的，也不能靠 VMAF 兜住：

* 交错源直接当渐进帧编码，梳齿会被永久烙进画面，交错标记还会丢（AV1 没有交错
  信号）。而 VMAF 是拿梳齿比梳齿，照样打 94 分——指标完全看不出来；
* 死黑边是纯黑像素，占着 20% 以上的画幅却不含任何信息，编码器会老老实实为它
  编码（虽然很便宜），裁掉是几乎无损的体积收益。

所以在编码之前先解一遍源文件，用 idet 判扫描方式、cropdetect 求所有帧的内容
包围盒，然后生成一份「这个文件的滤镜链 + 目标几何」的计划。滤镜链会同时用在
主编码和 VMAF 参考段抽取上——两边必须是同一个口径，否则比的是两个东西。
"""

import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from . import proc
from .util import to_int

# 一行 cropdetect 的结尾：crop=w:h:x:y。reset=0 时这些值是「到目前为止所有帧的
# 内容包围盒」，所以最后一行就是全片的答案。
_CROP_RE = re.compile(r"crop=(\d+):(\d+):(-?\d+):(-?\d+)")
_IDET_MULTI_RE = re.compile(
    r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*"
    r"Progressive:\s*(\d+)\s*Undetermined:\s*(\d+)")
_IDET_REPEAT_RE = re.compile(
    r"Repeated Fields:\s*Neither:\s*(\d+)\s*Top:\s*(\d+)\s*Bottom:\s*(\d+)")

# 交错判定：被判成场交错的帧占比超过这个值就认定是交错源。取一半是因为混合
# 素材（片头渐进、正片交错）也该按交错处理，反交错滤镜对渐进帧是空操作。
INTERLACE_RATIO = 0.5
# 重复场占比超过这个值就是 telecine（3:2 pulldown），不是普通交错
TELECINE_RATIO = 0.15
# ffprobe 里表示交错的 field_order 取值
INTERLACED_FIELD_ORDERS = {"tt", "bb", "tb", "bt", "interlaced"}


@dataclass(frozen=True)
class SourcePlan:
    """一个文件的处理计划。filters 会同时作用于主编码和 VMAF 参考段。"""

    filters: tuple = ()
    width: int = 0          # 滤镜链之后的目标几何（0 表示不变）
    height: int = 0
    notes: tuple = ()       # 给日志用的人话说明
    skip_reason: str = ""   # 非空表示这个文件不该压

    @property
    def vf(self) -> str:
        return ",".join(self.filters)

    def expected_size(self, info) -> tuple:
        w = self.width or info.width
        h = self.height or info.height
        return w, h


@dataclass
class Scan:
    """一次探查的原始结果，判定逻辑与解析分开，方便单独测。"""

    tff: int = 0
    bff: int = 0
    progressive: int = 0
    undetermined: int = 0
    repeat_neither: int = 0
    repeat_top: int = 0
    repeat_bottom: int = 0
    crop: tuple = ()        # (w, h, x, y)
    ok: bool = False
    error: str = ""

    @property
    def frames(self) -> int:
        return self.tff + self.bff + self.progressive + self.undetermined

    @property
    def interlaced_ratio(self) -> float:
        return (self.tff + self.bff) / self.frames if self.frames else 0.0

    @property
    def repeat_ratio(self) -> float:
        total = self.repeat_neither + self.repeat_top + self.repeat_bottom
        return (self.repeat_top + self.repeat_bottom) / total if total else 0.0


def scan_cmd(ffmpeg: str, src: Path, crop_limit: int, want_idet: bool,
             want_crop: bool) -> list:
    """一次解码同时跑 idet 和 cropdetect：解码是这里唯一的成本，别解两遍。"""
    chain = []
    if want_idet:
        chain.append("idet")
    if want_crop:
        # reset=0 让 cropdetect 累积所有帧的最大内容框，得到的裁切框对每一帧都
        # 成立；只抽几个位置的话，只在某一场戏出现的硬字幕会被裁掉。
        chain.append(f"cropdetect=limit={crop_limit}:round=2:reset=0")
    return [
        ffmpeg, "-hide_banner", "-nostats",
        "-i", str(src),
        "-map", "0:V:0",
        "-vf", ",".join(chain),
        "-an", "-sn", "-dn",
        "-f", "null", "-",
    ]


def parse_scan(lines) -> Scan:
    """从 ffmpeg 日志里取出 idet 汇总和最后一个 cropdetect 包围盒。

    idet 的汇总行可能出现多次（滤镜实例化多次），取帧数最多的那一组。
    """
    scan = Scan()
    best_frames = -1
    last_crop = None
    for line in lines:
        m = _IDET_MULTI_RE.search(line)
        if m:
            tff, bff, prog, und = (int(x) for x in m.groups())
            if tff + bff + prog + und > best_frames:
                best_frames = tff + bff + prog + und
                scan.tff, scan.bff = tff, bff
                scan.progressive, scan.undetermined = prog, und
            continue
        m = _IDET_REPEAT_RE.search(line)
        if m:
            neither, top, bottom = (int(x) for x in m.groups())
            if neither + top + bottom >= scan.repeat_neither + \
                    scan.repeat_top + scan.repeat_bottom:
                scan.repeat_neither, scan.repeat_top = neither, top
                scan.repeat_bottom = bottom
            continue
        m = _CROP_RE.search(line)
        if m:
            last_crop = tuple(int(x) for x in m.groups())
    if last_crop and last_crop[0] > 0 and last_crop[1] > 0:
        scan.crop = last_crop
    return scan


def run_scan(tools, src: Path, cfg: dict, log=None) -> Scan:
    """解一遍源文件做探查。日志按帧刷，所以流式读、只留有用的几行。"""
    acfg = cfg["analyze"]
    want_idet = str(acfg.get("deinterlace", "auto")).lower() != "off" or \
        bool(acfg.get("skip_telecine", True))
    want_crop = str(acfg.get("crop", "auto")).lower() != "off"
    if not (want_idet or want_crop):
        return Scan(ok=True)
    cmd = scan_cmd(tools.ffmpeg, src, to_int(acfg.get("crop_limit"), 16),
                   want_idet, want_crop)
    if log:
        log.info(f"analyzing {src.name} (one decode pass: "
                 f"{'idet ' if want_idet else ''}{'cropdetect' if want_crop else ''})")
    process = proc.spawn(cmd)
    kept = deque(maxlen=64)
    last_crop_line = None
    try:
        for raw in process.stderr:
            line = raw.strip()
            if not line:
                continue
            if "cropdetect" in line:
                last_crop_line = line  # 每帧一行，只留最后一行（累积包围盒）
            elif "idet" in line or "detection" in line or "Repeated" in line:
                kept.append(line)
            elif "rror" in line:
                kept.append(line)
        rc = process.wait()
    except BaseException:
        try:
            process.kill()
        except OSError:
            pass
        raise
    finally:
        proc.unregister(process)
        try:
            process.stderr.close()
        except OSError:
            pass
        try:
            process.stdout.close()
        except OSError:
            pass
    lines = list(kept)
    if last_crop_line:
        lines.append(last_crop_line)
    scan = parse_scan(lines)
    scan.ok = rc == 0
    if rc != 0:
        scan.error = "; ".join(list(kept)[-3:]) or f"analysis exited {rc}"
    return scan


def crop_filter(scan: Scan, info, min_pixels: int):
    """把探查到的包围盒变成 crop 滤镜；不值得裁就返回 None。

    只裁掉纯黑边，且必须是「每一帧都黑」（cropdetect reset=0 保证了这点）。
    小于 min_pixels 的边不裁：几个像素的收益不值得动几何。
    """
    if not scan.crop or info.width <= 0 or info.height <= 0:
        return None
    w, h, x, y = scan.crop
    if w > info.width or h > info.height or w <= 0 or h <= 0:
        return None
    dropped_w, dropped_h = info.width - w, info.height - h
    if dropped_w < min_pixels and dropped_h < min_pixels:
        return None
    # 明显不合理的框（内容不足原画一半）宁可不动：可能是全黑片段把框压小了
    if w * 2 < info.width or h * 2 < info.height:
        return None
    if w % 2 or h % 2 or x % 2 or y % 2:
        return None  # 4:2:0 要求偶数几何，round=2 本该保证，保险起见再挡一层
    return f"crop={w}:{h}:{x}:{y}", w, h


def make_plan(scan: Scan, info, cfg: dict) -> SourcePlan:
    """把探查结果翻译成「怎么压这个文件」。"""
    acfg = cfg["analyze"]
    filters = []
    notes = []
    width = height = 0

    if not scan.ok:
        # 探查本身失败（源文件有问题）不该顶掉编码：按原样压，只记一条
        return SourcePlan(notes=(f"analysis failed ({scan.error[:120]}); "
                                 f"encoding the source as-is",))

    telecine = scan.repeat_ratio >= TELECINE_RATIO
    interlaced = scan.interlaced_ratio >= INTERLACE_RATIO or (
        scan.frames == 0 and
        (info.field_order or "").lower() in INTERLACED_FIELD_ORDERS)

    if telecine and bool(acfg.get("skip_telecine", True)):
        return SourcePlan(skip_reason=(
            f"telecined source ({scan.repeat_ratio:.0%} repeated fields); "
            f"needs inverse telecine, which this tool does not do"
        ))

    mode = str(acfg.get("deinterlace", "auto")).lower()
    if interlaced and mode != "off":
        # send_frame 而不是 send_field：一帧出一帧，帧数和帧率都不变，后面的
        # 时长/进度/VMAF 逐帧比对全都还成立。
        filters.append("bwdif=mode=send_frame:parity=auto:deint=all")
        notes.append(f"interlaced source ({scan.interlaced_ratio:.0%} of frames, "
                     f"field order {info.field_order or 'unknown'}); "
                     f"deinterlacing with bwdif")
    elif interlaced:
        notes.append("interlaced source but deinterlace is off; combing will be "
                     "baked into the output")

    if str(acfg.get("crop", "auto")).lower() != "off":
        cropped = crop_filter(scan, info, to_int(acfg.get("crop_min_pixels"), 8))
        if cropped:
            expr, width, height = cropped
            filters.append(expr)
            notes.append(f"black bars detected on every frame; cropping "
                         f"{info.width}x{info.height} -> {width}x{height}")

    return SourcePlan(filters=tuple(filters), width=width, height=height,
                      notes=tuple(notes))


def analyse(tools, src: Path, info, cfg: dict, log=None) -> SourcePlan:
    """对外入口：探查 + 生成计划。关掉 [analyze].enabled 就直接返回空计划。"""
    if not cfg["analyze"].get("enabled", True):
        return SourcePlan()
    scan = run_scan(tools, src, cfg, log)
    plan = make_plan(scan, info, cfg)
    if log:
        for note in plan.notes:
            log.info(f"[analyze] {src.name}: {note}")
    return plan
