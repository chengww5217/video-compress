"""配置加载与校验。

默认值内置在代码里（DEFAULTS），config.toml 只是覆盖层。这样做的好处：

* 用户的配置少写一节不会炸出 KeyError，缺什么补什么；
* 打包安装后没有 config.toml 也能跑；
* 写错键名（`min_saving_ration`）会立刻报错，而不是被 `.get(默认值)` 悄悄吃掉。
"""

import copy
import tomllib
from pathlib import Path

DEFAULTS = {
    "tools": {
        "ffmpeg": "",
        "ffprobe": "",
    },
    "skip": {
        "min_size_mb": 20,
        "min_saving_ratio": 0.15,
        "extensions": [".mp4", ".mkv", ".mov", ".avi", ".webm", ".ts",
                       ".m4v", ".flv", ".wmv", ".mpg", ".mpeg"],
        "tag_marker": "vcompress",
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
            "extra": "-svtav1-params tune=0:film-grain-denoise=0",
        },
        "x265": {
            "codec": "libx265",
            "preset": "placebo",
            "crf": 20,
            "extra": "-x265-params aq-mode=3:psy-rd=2.0",
        },
    },
    "adaptive": {
        "enabled": True,
        "target_vmaf": 95.0,
        "crf_min": 16,
        "crf_max": 44,
        "sample_seconds": 30,
        "search_step": 2,
    },
    "audio": {
        "transcode_over_kbps": 512,
        "opus_kbps": 160,
    },
    "verify": {
        "decode_check": True,
        "max_duration_diff_s": 0.5,
        "min_actual_saving": 0.05,
        "color_check": True,
        "vmaf_enabled": True,
        "vmaf_segments": 3,
        "vmaf_sample_seconds": 20,
        "vmaf_threshold": 93.0,
        "vmaf_min_segment": 90.0,
        "keep_failed_output": False,
    },
    "run": {
        "workers": 1,
        "results_file": "logs/results.jsonl",
        "log_file": "logs/vcompress.log",
        "log_max_bytes": 5_242_880,
        "log_backups": 3,
        "use_recycle_bin": False,
        "keep_original": False,
    },
}

# 编码器子表（[encode.av1] 这类）的键；用户可以新增编码器，键必须是这些
ENCODER_KEYS = {
    "codec": str,
    "preset": (str, int),
    "crf": int,
    "extra": str,
    # 留空表示按源片自动决定；只在源像素格式该编码器不支持时才需要手动指定
    "pix_fmt": str,
}
ENCODER_REQUIRED = ("codec", "preset", "crf")

# 键名自由的表：内容由用户扩展（编解码器名 -> 数值）
FREEFORM_TABLES = {("skip", "bpp"), ("skip", "expected_ratio")}

# 取值必须落在 [0, 1] 的比例项
RATIO_KEYS = {
    ("skip", "min_saving_ratio"),
    ("verify", "min_actual_saving"),
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
        errors.append(f"unknown option [{_name(path)}] {key}"
                      if path else f"unknown section [{key}]")


def _check_table(user: dict, template: dict, path: tuple, errors: list):
    _check_unknown(user, template, path, errors)
    for key, default in template.items():
        if key not in user:
            continue
        value = user[key]
        here = path + (key,)
        if isinstance(default, dict):
            if not isinstance(value, dict):
                errors.append(f"[{_name(here)}] must be a table")
            elif here in FREEFORM_TABLES:
                for k, v in value.items():
                    if not _type_ok(v, float):
                        errors.append(f"[{_name(here)}] {k} must be a number")
            else:
                _check_table(value, default, here, errors)
            continue
        expected = type(default)
        if not _type_ok(value, expected):
            errors.append(f"[{_name(path)}] {key} must be "
                          f"{expected.__name__}, got {type(value).__name__}")
        elif here in RATIO_KEYS and not 0.0 <= float(value) <= 1.0:
            errors.append(f"[{_name(path)}] {key} must be between 0 and 1")


def _check_encoders(cfg: dict, errors: list):
    names = encoder_names(cfg)
    if not names:
        errors.append("[encode] has no encoder table such as [encode.av1]")
        return
    for name in names:
        table = cfg["encode"][name]
        path = ("encode", name)
        for key in table:
            if key not in ENCODER_KEYS:
                errors.append(f"unknown option [{_name(path)}] {key}")
        for key in ENCODER_REQUIRED:
            if key not in table:
                errors.append(f"[{_name(path)}] is missing {key}")
        for key, expected in ENCODER_KEYS.items():
            if key in table and not _type_ok(table[key], expected):
                errors.append(f"[{_name(path)}] {key} has the wrong type")
    selected = cfg["encode"].get("encoder")
    if selected not in names:
        errors.append(f"[encode] encoder = {selected!r} has no matching "
                      f"[encode.{selected}] table (available: "
                      f"{', '.join(names)})")


def _check_semantics(cfg: dict, errors: list):
    ad = cfg["adaptive"]
    if ad["crf_min"] >= ad["crf_max"]:
        errors.append("[adaptive] crf_min must be smaller than crf_max")
    if not 0 < float(ad["target_vmaf"]) <= 100:
        errors.append("[adaptive] target_vmaf must be within (0, 100]")
    if int(ad["sample_seconds"]) <= 0:
        errors.append("[adaptive] sample_seconds must be positive")
    if int(ad["search_step"]) < 1:
        errors.append("[adaptive] search_step must be at least 1")

    vf = cfg["verify"]
    if not 0 < float(vf["vmaf_threshold"]) <= 100:
        errors.append("[verify] vmaf_threshold must be within (0, 100]")
    if not 0 <= float(vf["vmaf_min_segment"]) <= 100:
        errors.append("[verify] vmaf_min_segment must be within [0, 100] "
                      "(0 turns the per-segment floor off)")
    if float(vf["vmaf_min_segment"]) > float(vf["vmaf_threshold"]):
        errors.append("[verify] vmaf_min_segment must not exceed "
                      "vmaf_threshold; the floor is meant to catch a single "
                      "bad segment, not to be stricter than the average")
    if int(vf["vmaf_segments"]) < 1:
        errors.append("[verify] vmaf_segments must be at least 1")
    if float(vf["vmaf_sample_seconds"]) <= 0:
        errors.append("[verify] vmaf_sample_seconds must be positive")
    if float(vf["max_duration_diff_s"]) < 0:
        errors.append("[verify] max_duration_diff_s cannot be negative")

    if int(cfg["run"]["workers"]) < 1:
        errors.append("[run] workers must be at least 1")
    if int(cfg["run"]["log_backups"]) < 0:
        errors.append("[run] log_backups cannot be negative")

    ext = cfg["skip"]["extensions"]
    if not ext:
        errors.append("[skip] extensions is empty; nothing would be processed")
    for e in ext:
        if not isinstance(e, str) or not e.startswith("."):
            errors.append(f"[skip] extensions entry {e!r} must start with a dot")
    container = cfg["encode"]["output_container"]
    if not container.startswith("."):
        errors.append("[encode] output_container must start with a dot")
    if not cfg["skip"]["tag_marker"].strip():
        errors.append("[skip] tag_marker cannot be empty; it is what stops "
                      "files from being compressed twice")

    audio = cfg["audio"]
    if int(audio["opus_kbps"]) <= 0:
        errors.append("[audio] opus_kbps must be positive")
    if int(audio["transcode_over_kbps"]) <= 0:
        errors.append("[audio] transcode_over_kbps must be positive")


def validate(cfg: dict) -> dict:
    """校验一份已经和默认值合并过的配置，返回它自身；有问题抛 ConfigError。"""
    errors = []
    _check_unknown(cfg, DEFAULTS, (), errors)
    for section, template in DEFAULTS.items():
        if section not in cfg:
            errors.append(f"missing section [{section}]")
        elif not isinstance(cfg[section], dict):
            errors.append(f"[{section}] must be a table")
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
    return validate(cfg)


def load(path: Path, required: bool = True) -> dict:
    """读配置文件；required=False 时文件不存在就用内置默认值。"""
    if not path.exists():
        if required:
            raise ConfigError(f"config file not found: {path}")
        return normalise({})
    try:
        with open(path, "rb") as f:
            user = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from e
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    try:
        return normalise(user)
    except ConfigError as e:
        raise ConfigError(f"{path} is not a usable config:\n{e}") from e


def resolve_run_path(base: Path, value: str) -> Path:
    """[run] 里的相对路径按配置文件所在目录解析，而不是当前工作目录。"""
    p = Path(value).expanduser()
    return p if p.is_absolute() else base / p
