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
      校验：全量解码 / 时长 / 体积 / VMAF 抽检
                │通过                    │失败
                ▼                        ▼
      替换并删除原文件          删除临时文件，保留原件，记录日志
```

防重复压缩的三道保险：

1. 已处理文件会写入 `COMMENT=vcompress-v1` 元数据标记，再次遇到直接跳过；
2. 按编码器与「每像素码率」（BPP = 码率 ÷ 宽 × 高 × 帧率）判断是否已压到位；
3. 预估收益低于 15% 的不压；实际收益低于 5% 的丢弃结果、保留原件。

另外 `results.jsonl` 会记录每次运行结果，重跑时自动跳过上次已成功的文件。

## 环境要求

| 组件 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Windows 10/11 | 其他平台理论可用，未测试 |
| Python | ≥ 3.11 | 仅用标准库（需内置 `tomllib`） |
| ffmpeg / ffprobe | 需含 `libsvtav1`、`libx265` | 建议 `libvmaf` 也启用（自适应 CRF 和抽检依赖它） |
| Send2Trash（可选） | `pip install Send2Trash` | 使用 `--recycle` 回收站模式时需要；未安装时会退化为直接删除 |

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
python vcompress.py D:\videos --dry-run

# 2. 正式压缩整个目录（递归）
python vcompress.py D:\videos

# 3. 只处理单个或多个文件
python vcompress.py "D:\movies\a.mkv" "D:\movies\b.mp4"
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
| `--workers N` | 并发处理的文件数（默认 1；单文件编码本身已是多线程） |
| `--force` | 忽略 `results.jsonl` 缓存，重新处理所有文件（元数据标记仍生效） |
| `--recycle` | 删除原文件时进回收站而不是直接删除 |
| `--config 路径` | 指定配置文件（默认同目录下 `config.toml`） |

退出码：`0` 全部成功；`1` 存在失败；`130` 被 Ctrl+C 中断。

中断是安全的：临时文件会在下次运行开头被清理，原文件不会受损。

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

- `bpp` 表：各编码器的每像素码率下限，低于即视为已压到位
- `expected_ratio` 表：各编码器的预估压缩比，据此估算收益
- `min_size_mb = 20`：小文件跳过
- `extensions`：处理的容器白名单

### 音频 `[audio]`

默认直接复制（`-c:a copy`）；仅当音轨是无损格式（PCM/FLAC 等）或码率超过 `transcode_over_kbps`(512k) 时转 Opus `opus_kbps`(160k)。字幕、章节、附件全部保留。

### 校验 `[verify]`

| 检查项 | 默认 | 失败处理 |
|---|---|---|
| 全量解码无报错 | 开 | 放弃输出 |
| 时长偏差 ≤ 0.5s | 是 | 放弃输出 |
| 输出必须更小且省 ≥ 5% | 是 | 保留原件 |
| VMAF 抽检 3 段均值 ≥ 93 | 开 | 保留原件 |

任何一项不过都会删除临时文件并保留原文件，不会出现"压坏了还删了原件"的情况。

## 日志与记录

- `logs/vcompress.log`：完整日志（DEBUG 级，含每次 VMAF 探测值）
- `results.jsonl`：每个文件一行 JSON 的处理记录，用于断点续跑和统计

## 常见问题

**Q：为什么压缩很慢？**
默认 preset 2/placebo 是极限压缩档，一部电影可能需要数小时。这是设计取向（时间换体积）。想加速可调低 `[encode.av1].preset`（如 6~8），体积会稍大。

**Q：想更激进地缩小体积？**
把 `[adaptive].target_vmaf` 从 95 降到 92~93，或调高各编码器 `crf`。

**Q：某些播放设备放不了 AV1？**
改用 `python vcompress.py ... --encoder x265`，或在配置里永久切换。

**Q：如何强制重新压缩已处理过的文件？**
删除文件元数据里的标记较麻烦，直接改用一份 `min_saving_ratio = 1.0` 且清空 `expected_ratio`/`bpp` 的配置即可绕过跳过判定。
