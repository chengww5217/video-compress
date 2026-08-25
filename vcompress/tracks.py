"""音轨与字幕怎么处置，以及按这些决定预估产物体积。

和视频编码分开的理由：这里的每一条都是「哪条轨要动、动成什么」，而体积预估
必须和实际下发的参数用同一套决定——两边各写一份判断，慢慢就会走偏。
"""

MKV_LIKE = {".mkv", ".webm"}
MP4_LIKE = {".mp4", ".m4v", ".mov"}

# -------------------------------------------------------------------------- 音频

LOSSLESS_AUDIO = ("pcm_", "flac", "alac", "ape", "wavpack", "truehd", "mlp")
OPUS_MIN_KBPS = 48
OPUS_MAX_KBPS = 510
# libopus 只认这几种规范布局，5.1(side) 这类变体会让编码器打不开
OPUS_LAYOUTS = {1: "mono", 2: "stereo", 3: "3.0", 4: "quad",
                5: "5.0", 6: "5.1", 7: "6.1", 8: "7.1"}


def opus_bitrate(base_kbps: int, channels: int) -> int:
    """opus_kbps 以立体声为基准，按声道数线性缩放。"""
    ch = channels if channels > 0 else 2
    return max(OPUS_MIN_KBPS, min(OPUS_MAX_KBPS, round(base_kbps * ch / 2)))


def opus_layout_args(index: int, channels: int) -> list:
    """把声道布局规范化到 libopus 接受的形式。

    ffmpeg 的 libopus 封装会校验布局名：5.1(side)、unknown 这类直接报
    "Invalid channel layout ... for specified mapping family" 而整个转码失败。
    声道数已知就按规范布局重排，超过 8 声道退回任意映射。
    """
    layout = OPUS_LAYOUTS.get(channels)
    if layout:
        return [f"-filter:a:{index}", f"aformat=channel_layouts={layout}"]
    return [f"-mapping_family:a:{index}", "255"]


# 未压缩 PCM 转 FLAC 大致能省四成；TrueHD/ALAC 这些本身已经是无损压缩格式，
# 换成 FLAC 体积基本持平。这两个比例只用来预估体积，不影响实际编码。
FLAC_RATIO_PCM = 0.6
FLAC_RATIO_PACKED = 1.0


def flac_bitrate(codec: str, src_bitrate: int) -> int:
    if src_bitrate <= 0:
        return 0
    ratio = (FLAC_RATIO_PCM if (codec or "").lower().startswith("pcm_")
             else FLAC_RATIO_PACKED)
    return int(src_bitrate * ratio)


def audio_plan(info, cfg, container: str = ".mkv") -> list:
    """逐条音轨的处置决定：("copy"|"flac"|"opus", 码率 bps)，0 表示不知道。

    单独抽出来是为了让「产物会有多大」这个预估和实际编码参数用同一套决定，
    不会两边各写一份判断然后慢慢走偏。

    无损轨默认转 FLAC 而不是 Opus：这个工具压完是要删原文件的，把 TrueHD/PCM
    换成有损轨就再也换不回来了，而 FLAC 仍是数学无损、也能省三到四成。想要
    更小的体积就设 [audio] lossless = "opus"。FLAC 在 mp4 里播放器支持很差，
    所以只在 mkv 系容器里用它。
    """
    threshold = int(cfg["audio"].get("transcode_over_kbps", 512)) * 1000
    base_kbps = int(cfg["audio"].get("opus_kbps", 160))
    mode = str(cfg["audio"].get("lossless", "flac")).lower()
    if (container or "").lower() not in MKV_LIKE:
        mode = "opus"
    plan = []
    for i, codec in enumerate(info.audio_codecs):
        codec = (codec or "").lower()
        br = info.audio_bitrates[i] if i < len(info.audio_bitrates) else 0
        ch = info.audio_channels[i] if i < len(info.audio_channels) else 2
        if codec.startswith(LOSSLESS_AUDIO):
            if mode != "flac":
                plan.append(("opus", opus_bitrate(base_kbps, ch) * 1000))
            elif codec == "flac":
                plan.append(("copy", br))  # 已经是 FLAC，重编一遍毫无意义
            else:
                plan.append(("flac", flac_bitrate(codec, br)))
        elif br and br > threshold:
            plan.append(("opus", opus_bitrate(base_kbps, ch) * 1000))
        else:
            plan.append(("copy", br))
    return plan


def audio_args(info, cfg, container: str = ".mkv") -> list:
    """逐条音轨决定 copy / FLAC / Opus，不因为一条无损轨就重压所有音轨。"""
    if not info.audio_codecs:
        return []
    args = []
    transcoding = False
    for i, (kind, bitrate) in enumerate(audio_plan(info, cfg, container)):
        if kind == "opus":
            ch = info.audio_channels[i] if i < len(info.audio_channels) else 2
            args += [f"-c:a:{i}", "libopus",
                     f"-b:a:{i}", f"{bitrate // 1000}k"]
            args += opus_layout_args(i, ch)
            transcoding = True
        elif kind == "flac":
            # 时间不是约束，直接上最高压缩档
            args += [f"-c:a:{i}", "flac", f"-compression_level:a:{i}", "12"]
            transcoding = True
        else:
            args += [f"-c:a:{i}", "copy"]
    if not transcoding:
        return ["-c:a", "copy"]
    return args


# -------------------------------------------------------------------------- 字幕

# matroska 不认 mp4 系的文本字幕，mp4 只认 mov_text，跨容器复制会直接失败
MP4_TEXT_SUBS = {"mov_text", "tx3g"}
MKV_TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "webvtt", "text", "subviewer"}


def subtitle_args(info, container: str) -> list:
    """逐条字幕决定 copy 还是按目标容器转换文本字幕。"""
    if not info.subtitle_codecs:
        return []
    container = (container or "").lower()
    args = []
    for i, codec in enumerate(info.subtitle_codecs):
        codec = (codec or "").lower()
        if container in MKV_LIKE and codec in MP4_TEXT_SUBS:
            args += [f"-c:s:{i}", "srt"]
        elif container in MP4_LIKE and codec in MKV_TEXT_SUBS:
            args += [f"-c:s:{i}", "mov_text"]
        else:
            args += [f"-c:s:{i}", "copy"]
    return args


# ------------------------------------------------------------------------ 体积预估

def predicted_size(info, cfg, video_bitrate: int, container: str = ".mkv"):
    """按样本实测的视频码率预估产物体积；信息不全时返回 None。

    比 [skip].expected_ratio 那张按编码器写死的粗估准得多：那是"h264 大概能
    压到 55%"，这是"这个片源在选定 CRF 下实测每秒多少字节"。字幕/章节/附件
    的开销忽略不计，所以估出来偏小——宁可偏乐观，也不要把本来能省下来的文件
    误判成不值得压。
    """
    if video_bitrate <= 0 or info.duration <= 0:
        return None
    total = video_bitrate
    for _kind, br in audio_plan(info, cfg, container):
        if br <= 0:
            return None  # 有音轨码率不明，整个预估就不可信，宁可不猜
        total += br
    return int(total * info.duration / 8)
