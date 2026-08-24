# video-compress

基于 ffmpeg 的批量视频压缩工具。目标：**视觉无损**前提下尽可能压缩体积，压缩校验通过后才删除原文件，并通过元数据标记与码率阈值防止重复压缩。

## 工作流程

```
扫描视频 → ffprobe 探测 → 是否需要压缩？ ──否──→ 跳过（记录原因）
                │是
                ▼
      自适应 CRF 搜索（抽样 + VMAF 二分）
                ▼
      编码到 <名字>.tmp.mkv
                ▼
      校验：全量解码 / 时长 / 流数量 / 体积 / VMAF 抽检
                │通过                    │失败
                ▼                        ▼
      替换并删除原文件          删除临时文件，保留原件，记录日志
```

防重复压缩的三道保险：

1. 已处理文件会写入 `COMMENT=<tag_marker>-v1` 元数据标记（默认 `vcompress-v1`），再次遇到直接跳过；标记名跟着 `[skip].tag_marker` 走，改配置检测也随之变化；
2. 按编码器与「每像素码率」（BPP = 视频轨码率 ÷ 宽 × 高 × 帧率）判断是否已压到位。只算视频轨，容器没给就用总码率扣掉音轨估算，避免肥音轨把判断带偏；
3. 预估收益低于 `min_saving_ratio`（默认 15%）的不压；实际收益低于 5% 的丢弃结果、保留原件。

另外 `logs/results.jsonl` 会记录每次运行结果，重跑时自动跳过上次已成功的文件。

## 环境要求

| 组件 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Windows 10/11 | 其他平台理论可用，未测试 |
| Python | ≥ 3.11 | 仅用标准库（需内置 `tomllib`） |
| ffmpeg / ffprobe | 需含 `libsvtav1`、`libx265` | 建议同时带 `libvmaf` 和 `libx264`（自适应 CRF 与 VMAF 抽检依赖它们抽取无损样本片段） |
| Send2Trash（可选） | `pip install Send2Trash` | 使用 `--recycle` 回收站模式时需要；未安装时会退化为直接删除 |

启动时会自检一次 ffmpeg 构建能力：选定的视频编码器不存在就直接退出、不动任何文件；缺 `libvmaf`/`libx264`/`libopus` 只告警并说明会退化成什么行为。

### 安装 ffmpeg

已安装的可跳过。脚本按以下顺序自动查找：

1. `config.toml` 中 `[tools]` 手动指定的路径
2. 系统 `PATH`
3. `C:\Program Files\ffmpeg\bin`

可从 https://www.gyan.dev/ffmpeg/builds/ 下载 **full** 构建并解压到上述目录（或自行指定路径）。验证构建是否满足要求：

```powershell
ffmpeg -hide_banner -encoders | findstr "svtav1 x265"
ffmpeg -hide_banner -filters | findstr libvmaf
```

## 快速开始

```powershell
# 1. 先预览（不做任何修改，显示哪些会被压缩/跳过）
python -m vcompress D:\videos --dry-run

# 2. 正式压缩整个目录（递归）
python -m vcompress D:\videos

# 3. 只处理单个或多个文件
python -m vcompress "D:\movies\a.mkv" "D:\movies\b.mp4"

# 4. 只压缩、保留原文件（输出为 a.av1.mkv 这类副本）
python -m vcompress D:\videos --keep
```

> 新安装 Python 后需**重开终端**才能使用 `python` 命令。

## 命令行参数

| 参数 | 说明 |
|---|---|
| `paths...` | 要处理的视频文件或目录（至少一个） |
| `--dry-run` | 只分析并打印将要执行的操作，不改任何文件 |
| `--no-recursive` | 不递归子目录 |
| `--encoder av1\|x265` | 临时覆盖配置中的编码器 |
| `--no-adaptive` | 关闭自适应 CRF，直接用固定 CRF |
| `--workers N` | 并发处理的文件数（默认 1；单文件编码本身已是多线程）。真正并发时没有实时进度条，只保留各阶段日志；只有一个待处理文件时仍然串行执行并显示进度条 |
| `--force` | 忽略 `results.jsonl` 缓存，重新处理所有文件（元数据标记仍生效） |
| `--recycle` | 删除原文件时进回收站而不是直接删除 |
| `--keep` | 只生成压缩副本、不删原文件 |
| `--config 路径` | 指定配置文件（默认同目录下 `config.toml`） |

退出码：`0` 全部成功；`1` 存在失败；`130` 被 Ctrl+C 中断。

中断是安全的：Ctrl+C 会立刻终止所有 ffmpeg 子进程（并发模式也一样）、删掉半成品临时文件，原文件不受影响。漏下的临时文件会在下次处理同一个文件时清掉。

### 输出命名与覆盖保护

输出容器由 `[encode].output_container` 决定（默认 `.mkv`）：

- 常规情况 `a.mp4` → `a.mkv`，校验通过后才删除 `a.mp4`；`a.mkv` → 原地替换
- 目标名已经被**另一个文件**占用时，输出改名为 `a.<编码器><扩展名>`（如 `a.av1.mkv`）并打一条警告，绝不覆盖已有文件
- 连改名后的名字也被占用，这个文件直接报错跳过
- `--keep` 下输出与原文件同名，因此总是走改名规则

名字冲突在编码开始前就会判定，不会白跑几小时才失败。

## 配置说明（config.toml）

### 编码器选择 `[encode]`

- `av1`（默认）：SVT-AV1 `-preset 2 -crf 28`，同画质体积最小，速度极慢
- `x265`：libx265 `-preset placebo -crf 20`，兼容性更好，速度慢

两者都是"最慢=最小"的极限档位。preset 只影响压缩效率不影响画质，无需按片源调整。

### 自适应 CRF `[adaptive]`

默认开启。对每个视频从中间抽取样本片段，用 VMAF 二分搜索「满足目标画质的最小码率」对应的 CRF：

- `target_vmaf = 95`：视觉无损基准。想更激进可降到 92~93（体积更小、画质略降）
- `crf_min/crf_max`：搜索边界；搜索失败时回退固定 CRF

### 跳过规则 `[skip]`

- `bpp` 表：各编码器的每像素码率下限，低于即视为已压到位（只按视频轨码率算）
- `expected_ratio` 表：各编码器的预估压缩比，据此估算收益
- `min_saving_ratio = 0.15`：预估收益低于此值不压
- `min_size_mb = 20`：小文件跳过
- `extensions`：处理的容器白名单
- `tag_marker = "vcompress"`：写入和识别用的元数据标记名，两边始终一致

### 音频与其它轨道 `[audio]`

逐条音轨判断：普通有损轨直接复制，只有无损格式（PCM/FLAC/ALAC/TrueHD 等）或码率超过 `transcode_over_kbps`(512k) 的那一条转 Opus，不会因为一条无损轨就把整个文件的音轨全部重压。

Opus 码率以 `opus_kbps`(160k) 作为**立体声**基准按声道数缩放：单声道 80k、5.1 得 480k、7.1 封顶 510k。

字幕、章节、附件都会保留。文本字幕按目标容器自动转换（进 mkv 转 `srt`，进 mp4 转 `mov_text`），图形字幕（PGS/VobSub）原样复制。matroska 装不下的 data 流（如 mp4/mov 里的 tmcd 时间码轨）会被丢弃——否则整个转码会直接失败。

### 校验 `[verify]`

| 检查项 | 默认 | 失败处理 |
|---|---|---|
| 全量解码无报错 | 开 | 放弃输出 |
| 时长偏差 ≤ 0.5s | 是 | 放弃输出 |
| 音轨、字幕条数与原文件一致 | 是 | 放弃输出 |
| 输出必须更小且省 ≥ 5% | 是 | 保留原件 |
| VMAF 抽检 3 段均值 ≥ 93 | 开 | 保留原件 |

任何一项不过都会删除临时文件并保留原文件，不会出现"压坏了还删了原件"的情况。ffmpeg 构建里没有 libvmaf 时抽检会跳过并打警告，其余检查照常执行。

`--keep` 也可在配置中设为默认：在 `[run]` 节加 `keep_original = true`。

## 日志与记录

- `logs/vcompress.log`：完整日志（DEBUG 级，含每次 VMAF 探测值）
- `logs/results.jsonl`：每个文件一行 JSON 的处理记录，用于断点续跑和统计

`[run]` 里这两个路径写相对路径时，按 **配置文件所在目录** 解析（不是当前工作目录），所以从任何目录运行都读写同一份记录；写绝对路径或 `~/...` 也支持。

## 项目结构

```
video-compress/
├── config.toml          # 配置文件
├── vcompress/           # 主包
│   ├── __main__.py      # 入口（python -m vcompress）
│   ├── cli.py           # 命令行解析、调度、输出命名与覆盖保护
│   ├── probe.py         # ffprobe 探测与跳过判定
│   ├── encode.py        # 编码参数生成与执行、抽样、自适应 CRF
│   └── verify.py        # 解码校验、流数量比对与 VMAF 抽检
└── tests/               # 测试（unittest）
    ├── helpers.py           # 配置与 VideoInfo 替身
    ├── test_config.py       # 随包配置与代码读取结构的一致性
    ├── test_probe.py        # 码率估算与跳过判定
    ├── test_encode.py       # 参数生成、构建能力探测、子进程行为
    ├── test_verify.py       # 校验与 VMAF 判定
    ├── test_cli.py          # 输出命名、路径解析、临时文件清理
    └── test_integration.py  # 真实 ffmpeg 端到端（缺 ffmpeg 自动跳过）
```

## 开发与测试

```powershell
# 运行全部测试（单元测试无需 ffmpeg；集成测试缺 ffmpeg 时自动跳过）
python -m unittest discover -s tests -v

# 只跑集成测试
python -m unittest tests.test_integration -v
```

## 常见问题

**Q：为什么压缩很慢？**
默认 preset 2/placebo 是极限压缩档，一部电影可能需要数小时。这是设计取向（时间换体积）。想加速可调低 `[encode.av1].preset`（如 6~8），体积会稍大。

**Q：想更激进地缩小体积？**
把 `[adaptive].target_vmaf` 从 95 降到 92~93，或调高各编码器 `crf`。

**Q：某些播放设备放不了 AV1？**
改用 `python -m vcompress ... --encoder x265`，或在配置里永久切换。

**Q：如何强制重新压缩已处理过的文件？**
`--force` 只忽略 `results.jsonl` 缓存，元数据标记依然生效。要连标记一起绕过，把 `[skip].tag_marker` 改成另一个值（检测跟着配置走），必要时再清空 `expected_ratio`/`bpp` 两张表。
