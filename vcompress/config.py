"""配置加载与校验。

默认值内置在代码里（DEFAULTS），config.toml 只是覆盖层。这样做的好处：

* 用户的配置少写一节不会炸出 KeyError，缺什么补什么；
* 打包安装后没有 config.toml 也能跑；
* 写错键名（`min_saving_ration`）会立刻报错，而不是被 `.get(默认值)` 悄悄吃掉。
"""

import copy
import tomllib
from pathlib import Path

from .files import SORT_ORDERS

DEFAULTS = {
    "tools": {
        "ffmpeg": "",
        "ffprobe": "",
    },
    "skip": {
        "min_size_mb": 20,
        "min_saving_ratio": 0.15,
        # .ts 不在默认列表里：它和 TypeScript 源码撞扩展名，扫代码目录时会把
        # 一堆 .ts 源文件当成 Transport Stream 视频收集。真压 TS 流请在配置里加回
        "extensions": [".mp4", ".mkv", ".mov", ".avi", ".webm",
                       ".m4v", ".flv", ".wmv", ".mpg", ".mpeg"],
        "tag_marker": "vcompress",
        # 交错源（DVD / 电视台 rip）直接跳过：压过去会丢掉交错标记，属于
        # VMAF 测不出来的视觉有损。写 false 表示照压（自己清楚后果）
        "skip_interlaced": True,
        "bpp": {
            "h264": 0.05, "hevc": 0.03, "vp9": 0.03, "av1": 0.02,
            "mpeg2video": 0.12, "mpeg4": 0.08,
        },
        "expected_ratio": {
            "h264": 0.55, "hevc": 0.78, "vp9": 0.80, "av1": 0.92,
            "mpeg2video": 0.40, "mpeg4": 0.45, "flv1": 0.45, "wmv3": 0.50,
        },
    },
    "encode": {
        "encoder": "av1",
        "output_container": ".mkv",
        "av1": {
            "codec": "libsvtav1",
            "preset": "2",
            "crf": 28,
            "min_bit_depth": 10,
            "extra":
                "-svtav1-params tune=0:enable-variance-boost=1:variance-boost-strength=1:variance-octile=4:qp-scale-compress-strength=1:luminance-qp-bias=10:sharpness=1:ac-bias=0.25:tf-strength=1:enable-dlf=2:scd=1:keyint=257",
        },
        "x265": {
            "codec": "libx265",
            "preset": "placebo",
            "crf": 20,
            "min_bit_depth": 10,
            "extra": "-x265-params aq-mode=3:psy-rd=2.0",
        },
    },
    "adaptive": {
        "enabled": True,
        "target_vmaf": 95.0,
        # 搜索余量：样本最差段要过 target_vmaf + target_margin 才录取。
        # 二分搜索必然收敛到录取线上（实测 45 次搜索的成绩全落在录取线上方
        # 0.15 分以内），所以这个余量要吸收的是「样本外推到全片」的全部误差，
        # 不是四舍五入的零头。
        "target_margin": 0.5,
        "crf_min": 16,
        "crf_max": 50,
        # 样本段数必须和 [verify] vmaf_segments 一致，两边的取样点才落在同一
        # 批位置上（都以 (i+0.5)/段数 分位为中心）；段长比抽检短，搜索的窗口
        # 就正好嵌在抽检窗口的正中间，量的是同一段内容。
        "sample_seconds": 8,
        "sample_count": 5,
        "search_step": 0.25,
        "require_target": True,
    },
    "audio": {
        "transcode_over_kbps": 512,
        "opus_kbps": 160,
        # 无损轨怎么处理："flac" 保持数学无损，"opus" 体积更小但不可逆
        "lossless": "flac",
    },
    "remux": {
        # 重封装模式（--remux）：修「检测不用再压、但快进卡顿」的文件。
        # 卡顿主因是关键帧间隔（GOP）太长——播放器 seek 要从上一个关键帧
        # 逐帧解到目标位置；其次是 avi/flv/wmv/mpg 这类没有可靠索引的容器
        "max_keyframe_gap_s": 5.0,
        # GOP 重编的画质闸门：比压缩的 95 更严——二次编码的代际损失要留余量
        "target_vmaf": 98.0,
        # 允许体积最多增长多少：关键帧不免费，多出来的就是修快进的代价
        "max_growth_ratio": 0.10,
    },
    "verify": {
        "decode_check": True,
        "max_duration_diff_s": 0.5,
        "min_actual_saving": 0.05,
        "color_check": True,
        "vmaf_enabled": True,
        "vmaf_segments": 5,
        "vmaf_sample_seconds": 20,
        "vmaf_threshold": 93.0,
        "vmaf_min_segment": 90.0,
        "vmaf_4k_model": True,
        "keep_failed_output": False,
    },
    "run": {
        "workers": 1,
        # 文件处理顺序，--sort 可临时覆盖；取值见 files.SORT_ORDERS
        "sort": "name",
        "results_file": "logs/results.jsonl",
        "log_file": "logs/vcompress.log",
        # 无损样本片段放哪里。留空 = 系统临时目录，但那在多数发行版上是
        # 内存盘（tmpfs）：4K 片源的无损抽样约 500 Mbps，一段 20 秒 1.2 GB，
        # 抽检要两份、搜索还要留几段，几个 G 的内存就这么没了，撑爆之后
        # 抽样失败。默认放到项目目录下，和日志、记录在一块。
        "temp_dir": "logs/tmp",
        "log_max_bytes": 5_242_880,
        "log_backups": 3,
        "use_recycle_bin": False,
        "delete_original": False,
        # 全部任务结束后自动关机。故意只做成配置项（命令行没有对应参数），
        # 而且是一次性的：关机前会被写回 false，下次要关必须再手动打开。
        # 这个值在所有任务结束时会重新读一遍，压缩过程中改主意也来得及。
        "shutdown_when_done": False,
        "shutdown_delay_seconds": 60,
    },
}

# 编码器子表（[encode.av1] 这类）的键；用户可以新增编码器，键必须是这些
ENCODER_KEYS = {
    "codec": str,
    "preset": (str, int),
    "crf": (int, float),
    "extra": str,
    # 留空表示按源片自动决定；只在源像素格式该编码器不支持时才需要手动指定
    "pix_fmt": str,
    # 位深下限：8-bit 源提到 10-bit 编码（精度更高、更不容易 banding）；
    # 写 8 表示照源片位深编
    "min_bit_depth": int,
}
ENCODER_REQUIRED = ("codec", "preset", "crf")

# 键名自由的表：内容由用户扩展（编解码器名 -> 数值）
FREEFORM_TABLES = {("skip", "bpp"), ("skip", "expected_ratio")}

# 取值必须落在 [0, 1] 的比例项
RATIO_KEYS = {
    ("skip", "min_saving_ratio"),
    ("verify", "min_actual_saving"),
    ("remux", "max_growth_ratio"),
}


class ConfigError(Exception):
    pass


def defaults() -> dict:
    return copy.deepcopy(DEFAULTS)


def encoder_names(cfg: dict) -> list:
    """[encode] 下所有子表就是可用的编码器名，不在代码里硬编码。"""
    return sorted(k for k, v in cfg.get("encode", {}).items()
                  if isinstance(v, dict))


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _type_ok(value, expected) -> bool:
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is float:
        # 写成 95 而不是 95.0 是常见且无害的
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected is str:
        return isinstance(value, str)
    if expected is list:
        return isinstance(value, list)
    if isinstance(expected, tuple):
        return any(_type_ok(value, e) for e in expected)
    return True


def _name(path: tuple) -> str:
    return ".".join(path)


def _check_unknown(user: dict, template: dict, path: tuple, errors: list):
    known = set(template)
    for key in user:
        if key in known:
            continue
        if path == ("encode",) and isinstance(user[key], dict):
            continue  # 用户自定义的编码器子表
        errors.append(f"[{_name(path)}] 里没有 {key} 这个配置项"
                      if path else f"没有 [{key}] 这个配置节")


def _check_table(user: dict, template: dict, path: tuple, errors: list):
    _check_unknown(user, template, path, errors)
    for key, default in template.items():
        if key not in user:
            continue
        value = user[key]
        here = path + (key,)
        if isinstance(default, dict):
            if not isinstance(value, dict):
                errors.append(f"[{_name(here)}] 必须是一个配置节（table）")
            elif here in FREEFORM_TABLES:
                for k, v in value.items():
                    if not _type_ok(v, float):
                        errors.append(f"[{_name(here)}] {k} 必须是数字")
            else:
                _check_table(value, default, here, errors)
            continue
        expected = type(default)
        if not _type_ok(value, expected):
            errors.append(f"[{_name(path)}] {key} 必须是 "
                          f"{expected.__name__}，现在是 {type(value).__name__}")
        elif here in RATIO_KEYS and not 0.0 <= float(value) <= 1.0:
            errors.append(f"[{_name(path)}] {key} 必须在 0 和 1 之间")


def _check_encoders(cfg: dict, errors: list):
    names = encoder_names(cfg)
    if not names:
        errors.append("[encode] 下面一个编码器配置节都没有（比如 [encode.av1]）")
        return
    for name in names:
        table = cfg["encode"][name]
        path = ("encode", name)
        for key in table:
            if key not in ENCODER_KEYS:
                errors.append(f"[{_name(path)}] 里没有 {key} 这个配置项")
        for key in ENCODER_REQUIRED:
            if key not in table:
                errors.append(f"[{_name(path)}] 缺少必填项 {key}")
        for key, expected in ENCODER_KEYS.items():
            if key in table and not _type_ok(table[key], expected):
                errors.append(f"[{_name(path)}] {key} 的类型不对")
        depth = table.get("min_bit_depth")
        if isinstance(depth, int) and not isinstance(depth, bool) \
                and not 8 <= depth <= 16:
            errors.append(f"[{_name(path)}] min_bit_depth 必须在 [8, 16] 之间"
                          f"（写 8 表示照源片位深编）")
    selected = cfg["encode"].get("encoder")
    if selected not in names:
        errors.append(f"[encode] encoder = {selected!r} 没有对应的 "
                      f"[encode.{selected}] 配置节（可用的有："
                      f"{', '.join(names)}）")


def _check_semantics(cfg: dict, errors: list):
    ad = cfg["adaptive"]
    if ad["crf_min"] >= ad["crf_max"]:
        errors.append("[adaptive] crf_min 必须小于 crf_max")
    if not 0 < float(ad["target_vmaf"]) <= 100:
        errors.append("[adaptive] target_vmaf 必须在 (0, 100] 之间")
    if not 0 <= float(ad["target_margin"]) <= 1:
        errors.append("[adaptive] target_margin 必须在 [0, 1] 之间"
                      "（单位是 VMAF 分，写 0 关闭余量）")
    if int(ad["sample_seconds"]) <= 0:
        errors.append("[adaptive] sample_seconds 必须是正数")
    if int(ad["sample_count"]) < 1:
        errors.append("[adaptive] sample_count 至少是 1")
    if float(ad["search_step"]) < 0.25:
        errors.append("[adaptive] search_step 至少是 0.25"
                      "（CRF 的实际可用粒度）")

    vf = cfg["verify"]
    if not 0 < float(vf["vmaf_threshold"]) <= 100:
        errors.append("[verify] vmaf_threshold 必须在 (0, 100] 之间")
    if not 0 <= float(vf["vmaf_min_segment"]) <= 100:
        errors.append("[verify] vmaf_min_segment 必须在 [0, 100] 之间"
                      "（写 0 表示关掉单段下限）")
    if float(vf["vmaf_min_segment"]) > float(vf["vmaf_threshold"]):
        errors.append("[verify] vmaf_min_segment 不能高于 vmaf_threshold："
                      "单段下限是用来抓某一段崩掉的，不该比均值门槛还严")
    if int(vf["vmaf_segments"]) < 1:
        errors.append("[verify] vmaf_segments 至少是 1")
    if float(vf["vmaf_sample_seconds"]) <= 0:
        errors.append("[verify] vmaf_sample_seconds 必须是正数")
    if float(vf["max_duration_diff_s"]) < 0:
        errors.append("[verify] max_duration_diff_s 不能是负数")

    rm = cfg["remux"]
    if float(rm["max_keyframe_gap_s"]) <= 0:
        errors.append("[remux] max_keyframe_gap_s 必须是正数（秒）")
    if not 0 < float(rm["target_vmaf"]) <= 100:
        errors.append("[remux] target_vmaf 必须在 (0, 100] 之间"
                      "（重封装是二次编码，画质闸门不该比压缩模式松）")

    if int(cfg["run"]["workers"]) < 1:
        errors.append("[run] workers 至少是 1")
    if cfg["run"]["sort"] not in SORT_ORDERS:
        errors.append(f"[run] sort 只能是：{'/'.join(SORT_ORDERS)}")
    if int(cfg["run"]["log_backups"]) < 0:
        errors.append("[run] log_backups 不能是负数")
    if int(cfg["run"]["shutdown_delay_seconds"]) < 0:
        errors.append("[run] shutdown_delay_seconds 不能是负数")

    ext = cfg["skip"]["extensions"]
    if not ext:
        errors.append("[skip] extensions 是空的，那样什么文件都不会被处理")
    for e in ext:
        if not isinstance(e, str) or not e.startswith("."):
            errors.append(f"[skip] extensions 里的 {e!r} 必须以点开头")
    container = cfg["encode"]["output_container"]
    if not container.startswith("."):
        errors.append("[encode] output_container 必须以点开头")
    if not cfg["skip"]["tag_marker"].strip():
        errors.append("[skip] tag_marker 不能为空：它就是防止同一个文件被"
                      "压第二遍的标记")

    audio = cfg["audio"]
    if int(audio["opus_kbps"]) <= 0:
        errors.append("[audio] opus_kbps 必须是正数")
    if int(audio["transcode_over_kbps"]) <= 0:
        errors.append("[audio] transcode_over_kbps 必须是正数")
    if str(audio["lossless"]).lower() not in ("flac", "opus"):
        errors.append('[audio] lossless 只能是 "flac" 或 "opus"')


def validate(cfg: dict) -> dict:
    """校验一份已经和默认值合并过的配置，返回它自身；有问题抛 ConfigError。"""
    errors = []
    _check_unknown(cfg, DEFAULTS, (), errors)
    for section, template in DEFAULTS.items():
        if section not in cfg:
            errors.append(f"缺少配置节 [{section}]")
        elif not isinstance(cfg[section], dict):
            errors.append(f"[{section}] 必须是一个配置节（table）")
        else:
            _check_table(cfg[section], template, (section,), errors)
    if errors:
        raise ConfigError("\n".join("  - " + e for e in errors))
    _check_encoders(cfg, errors)
    _check_semantics(cfg, errors)
    if errors:
        raise ConfigError("\n".join("  - " + e for e in errors))
    return cfg


def normalise(user_cfg: dict) -> dict:
    """合并默认值并校验，同时把大小写敏感的地方规范化。"""
    cfg = _merge(DEFAULTS, user_cfg or {})
    cfg["encode"]["output_container"] = cfg["encode"]["output_container"].lower()
    cfg["skip"]["extensions"] = [e.lower() for e in cfg["skip"]["extensions"]]
    # 用户新增的编码器子表也要拿到可选键的默认值
    for name in encoder_names(cfg):
        cfg["encode"][name].setdefault("extra", "")
        cfg["encode"][name].setdefault("min_bit_depth", 10)
    return validate(cfg)


def load(path: Path, required: bool = True) -> dict:
    """读配置文件；required=False 时文件不存在就用内置默认值。"""
    if not path.exists():
        if required:
            raise ConfigError(f"配置文件不存在：{path}")
        return normalise({})
    try:
        with open(path, "rb") as f:
            user = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} 不是合法的 TOML：{e}") from e
    except OSError as e:
        raise ConfigError(f"读不了 {path}：{e}") from e
    try:
        return normalise(user)
    except ConfigError as e:
        raise ConfigError(f"{path} 里的配置有问题：\n{e}") from e


def resolve_run_path(base: Path, value: str) -> Path:
    """[run] 里的相对路径按配置文件所在目录解析，而不是当前工作目录。"""
    p = Path(value).expanduser()
    return p if p.is_absolute() else base / p
