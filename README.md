# video-compress

基于 ffmpeg 的批量视频压缩工具。目标：**视觉无损**前提下尽可能压缩体积，压缩校验通过后才删除原文件，并通过元数据标记与码率阈值防止重复压缩。

## 工作流程

```
扫描视频 → ffprobe 探测 → 是否需要压缩？ ──否──→ 跳过（记录原因）
                │是
                ▼
      自适应 CRF 搜索（抽样 + VMAF 二分）
                ▼
      编码到 <源文件名>.tmp.mkv
                ▼
      校验：全量解码 / 时长 / 分辨率 / 流数量 / 体积 / VMAF 抽检
                │通过                    │失败
                ▼                        ▼
      替换并删除原文件          删除临时文件，保留原件，记录日志
```

防重复压缩的三道保险：

1. 已处理文件会写入 `COMMENT=<tag_marker>-v1` 元数据标记（默认 `vcompress-v1`），再次遇到直接跳过；标记名跟着 `[skip].tag_marker` 走，改配置检测也随之变化。原有的 comment 会被保留，标记追加在后面（`原注释 | vcompress-v1`）；
2. 按编码器与「每像素码率」（BPP = 视频轨码率 ÷ 宽 × 高 × 帧率）判断是否已压到位。只算视频轨，容器没给就用总码率扣掉音轨估算，避免肥音轨把判断带偏；
3. 预估收益低于 `min_saving_ratio`（默认 15%）的不压；实际收益低于 5% 的丢弃结果、保留原件。

自适应搜索结束后还会再拦一道：拿样本在选定 CRF 下的实测码率推算全片体积，收益仍不到 `min_saving_ratio` 就直接跳过，不会白编几个小时最后被 5% 的门槛丢掉。这个预估比 `expected_ratio` 那张按编码器写死的表准得多（合成片源上实测误差 0.03%，真实片源会大一些）。同理，连 `crf_min` 都达不到 `target_vmaf` 的片源（通常是重颗粒片）默认也直接跳过——「视觉无损优先」的前提下，压出一个低于目标画质的产物没有意义；想照压可以设 `[adaptive] require_target = false`。

另外 `logs/results.jsonl` 会记录每次运行结果，重跑时自动跳过上次已成功的文件。跳过前会比对体积：同名文件被换成另一个片源时旧记录不再生效，会重新检查。

## 环境要求

| 组件 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Windows / macOS / Linux | 主要在 Windows 与 macOS 上使用 |
| Python | ≥ 3.11 | 仅用标准库（需内置 `tomllib`） |
| ffmpeg / ffprobe | 需含 `libsvtav1`、`libx265` | 建议同时带 `libvmaf` 和 `libx264`（自适应 CRF 与 VMAF 抽检依赖它们抽取无损样本片段；没有 libx264 会自动退回 ffv1，只是临时文件更大） |
| Send2Trash（可选） | `pip install "vcompress[recycle]"` | 使用 `--recycle` 回收站模式时需要；未安装时会退化为直接删除 |

启动时会自检一次 ffmpeg 构建能力：选定的视频编码器不存在就直接退出、不动任何文件；缺 `libvmaf`/`libx264`/`libopus` 只告警并说明会退化成什么行为。

### 安装 ffmpeg

已安装的可跳过。脚本按以下顺序自动查找：

1. `config.toml` 中 `[tools]` 手动指定的路径（**指定了但找不到会直接报错**，不会悄悄改用别的 ffmpeg）
2. 系统 `PATH`
3. 平台常见目录（Windows：`C:\Program Files\ffmpeg\bin`；macOS/Linux：`/opt/homebrew/bin`、`/usr/local/bin`、`/usr/bin`）

Windows 可从 https://www.gyan.dev/ffmpeg/builds/ 下载 **full** 构建；macOS 用 `brew install ffmpeg`。验证构建是否满足要求：

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

也可以装成命令用：

```bash
pip install .              # 或 pip install ".[recycle]"
vcompress ~/videos --dry-run
```

装成命令后如果找不到 `config.toml`，会使用代码内置的默认配置，日志与记录写到**当前工作目录**下的 `logs/`。

> 新安装 Python 后需**重开终端**才能使用 `python` 命令。

## 命令行参数

| 参数 | 说明 |
|---|---|
| `paths...` | 要处理的视频文件或目录（至少一个） |
| `--dry-run` | 只分析并打印将要执行的操作，不改任何文件 |
| `--no-recursive` | 不递归子目录 |
| `--encoder 名字` | 临时覆盖配置中的编码器；名字必须有对应的 `[encode.<名字>]` 配置节 |
| `--no-adaptive` | 关闭自适应 CRF，直接用固定 CRF |
| `--workers N` | 并发处理的文件数（默认 1；单文件编码本身已是多线程）。真正并发时没有实时进度条，只保留各阶段日志；只有一个待处理文件时仍然串行执行并显示进度条 |
| `--force` | 忽略 `results.jsonl` 缓存，重新处理所有文件（元数据标记仍生效） |
| `--recycle` | 删除原文件时进回收站而不是直接删除 |
| `--keep` | 只生成压缩副本、不删原文件 |
| `--config 路径` | 指定配置文件（默认同目录下 `config.toml`；显式指定但文件不存在会报错退出） |

退出码：`0` 全部成功；`1` 存在失败；`2` 配置或环境有问题（没跑任何编码）；`130` 被 Ctrl+C 中断。

中断是安全的：Ctrl+C 会立刻终止所有 ffmpeg 子进程、删掉半成品临时文件，原文件不受影响。串行和并发两种模式都一样。漏下的临时文件会在下次处理同一个文件时清掉。

### 输出命名与覆盖保护

输出容器由 `[encode].output_container` 决定（默认 `.mkv`）：

- 常规情况 `a.mp4` → `a.mkv`，校验通过后才删除 `a.mp4`；`a.mkv` → 原地替换
- 目标名已经被**另一个文件**占用时，输出改名为 `a.<编码器><扩展名>`（如 `a.av1.mkv`）并打一条警告，绝不覆盖已有文件
- 连改名后的名字也被占用，这个文件直接报错跳过
- `--keep` 下输出不会指向原文件；同名时走上面的改名规则

名字冲突在编码开始前就会判定，不会白跑几小时才失败。并发模式下输出名会在内存里预留：同目录的 `a.mp4` 和 `a.avi` 都想产出 `a.mkv` 时，第二个自动让位到 `a.av1.mkv`，两个 worker 不会抢同一个名字。

临时文件名是 `<源文件完整名>.tmp<扩展名>`（如 `a.mp4.tmp.mkv`）。带上源文件的扩展名是必要的：只用主干名的话 `a.mp4` 和 `a.avi` 会共用一个 `a.tmp.mkv`，并发时两个 ffmpeg 往同一个文件写。

## 配置说明（config.toml）

所有默认值都内置在代码里（`vcompress/config.py` 的 `DEFAULTS`），`config.toml` 只是覆盖层：

- 少写一个配置节不会出错，缺的部分用默认值补齐；
- **写错键名会直接报错并指出是哪一个**（比如把 `min_saving_ratio` 打成 `min_saving_ration`），不会被静默忽略；
- 取值范围也会校验（比例必须在 0~1、`crf_min < crf_max`、`workers ≥ 1` 等）。

### 编码器选择 `[encode]`

- `av1`（默认）：SVT-AV1 `-preset 2 -crf 28`，同画质体积最小，速度极慢
- `x265`：libx265 `-preset placebo -crf 20`，兼容性更好，速度慢

两者都是"最慢=最小"的极限档位。preset 只影响压缩效率不影响画质，无需按片源调整。preset 2 已经在收益递减区，再往 0/1 压额外收益极小甚至可能倒退。

av1 节的 `extra` 是一组**偏主观画质**的参数（取自 SVT-AV1-Essential 这个"合理默认值"分支的调优结果，都在主线 SVT-AV1 里）：

| 参数 | 作用 |
|---|---|
| `tune=0` | VQ 档（主线默认是 PSNR 档），按人眼而不是按指标优化 |
| `enable-variance-boost=1` + `strength=1` `octile=4` | 给暗部和平坦低方差区域更多码率，正是最容易出 banding、而 VMAF 最测不出来的地方（主线默认关） |
| `qp-scale-compress-strength=1` | 压缩迷你 GOP 内各层的 QP 范围，帧间质量更一致，直接改善"最差帧" |
| `luminance-qp-bias=10` | 按帧平均亮度调 QP，专治暗场 |
| `sharpness=1` / `ac-bias=0.25` | 保纹理和颗粒锐度（x265 那边对应 `psy-rd`） |
| `tf-strength=1` | 削弱时域滤波（默认 3），少denoise、多保留细节 |
| `enable-dlf=2` | 更精确的去块滤波，只花时间不损画质 |
| `scd=1` + `keyint=257` | 场景切换处插关键帧，同时把 GOP 拉长到约 10 秒省码率；257 = 32×8+1，既贴合 mini-GOP 结构又不超过硬件解码器偏好的 300 帧 |

有个取舍要知道：`sharpness`/`ac-bias` 这类"保细节"的参数会让同 CRF 下的 VMAF 略降，于是自适应搜索会选一个更低的 CRF——**主观画质更好，文件比纯指标最优更大一点**。这符合"视觉无损优先、体积次之"的取向；想反过来（更小的文件、纯按指标最优）就把这两项删掉。

`extra` 里 SVT 不认识的键会被**静默忽略**（本机 ffmpeg 9 + SVT-AV1 4.2 实测拼错也照样编完），老一些的构建则可能直接报错。想确认参数真的生效，看编码日志里的 `SVT [config]` 那几行。

x265 节只显式写了 `aq-mode=3`（默认 2）和 `psy-rd=2.0`；`placebo` 本身已经把 `rd=6`/`rdoq=2`/`psy-rdoq=1.0` 拉满，再堆参数只是自我安慰。

想加第三个编码器，直接加一节 `[encode.vp9]`（至少给 `codec`/`preset`/`crf`），`--encoder vp9` 就能用，不需要改代码。

**像素格式**会自动适配：libsvtav1 只接受 4:2:0，遇到 4:2:2/4:4:4 的源（ProRes、部分 mov/avi）会自动降到同位深的 4:2:0 并打一条警告——不做这件事的话整个文件会直接编码失败。支持的格式是运行时问 ffmpeg 拿到的，不是硬编码表。需要手动控制可以在编码器节里写 `pix_fmt = "yuv420p10le"`。

**位深会提到 10-bit**（`min_bit_depth = 10`）：8-bit 源用 10-bit 编码是 AV1/HEVC 上公认的免费收益——编码器内部精度更高，暗部渐变不容易出现 banding，同画质下码率还略低。代价只是编解码稍慢，而 AV1 Main / HEVC Main10 都是必备档位，兼容性没有实际影响。只动位深不动色度：x265 上 4:2:2 的源会编成 `yuv422p10le` 而不是被顺手压成 4:2:0。写 `min_bit_depth = 8` 就照源片位深编。

### 自适应 CRF `[adaptive]`

默认开启。对每个视频从整片均匀抽 `sample_count`(4) 段、每段 `sample_seconds`(10) 秒，用 VMAF 二分搜索「满足目标画质的最小码率」对应的 CRF：

- 一个 CRF 的成绩取**最差段**，不取平均：平均分能被好段抬起来，观感却是最差处决定的。某段已经不达标时后面的段直接不量，省时间
- 只看中点一段是不够的：那个位置可能正好是明亮的动作戏或全黑的过场，按它定的 CRF 拿到暗场就不成立了。铺开取样后，搜索和后面用多段抽检的校验才是同一个口径
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
| 分辨率与原文件一致 | 是 | 放弃输出 |
| 音轨、字幕条数与原文件一致 | 是 | 放弃输出 |
| 输出必须更小且省 ≥ 5% | 是 | 保留原件 |
| 色彩属性与 HDR 母版信息一致 | 开 | 保留原件 |
| VMAF 抽检 3 段均值 ≥ 93 | 开 | 保留原件 |
| 且每一段都 ≥ 90 | 开 | 保留原件 |

检查按「先便宜后昂贵」的顺序跑：解码、时长、体积、色彩元数据都过了才会去做最贵的 VMAF 抽检。任何一项不过都会删除临时文件并保留原文件，不会出现"压坏了还删了原件"的情况。ffmpeg 构建里没有 libvmaf 时抽检会跳过并打警告，其余检查照常执行。

段内取**调和平均**而不是算术平均：算术平均会被大量好帧稀释，一段里几十帧崩到 60 分、均值照样 94。调和平均对低分帧敏感得多。段间仍取算术平均和 `vmaf_threshold` 比，但另有一条 `vmaf_min_segment`(90) 的**单段下限**——99/99/81 三段的均值是 93 能过阈值，可那 81 分的十几分钟是看得出来的。设 0 关掉单段下限。

`color_check`（默认开）比对 `color_primaries`/`color_transfer`/`color_space`/`color_range` 四项，以及 HDR10 母版信息（mastering display / content light level）、HDR10+、Dolby Vision RPU 是否还在。这项不能交给 VMAF：元数据丢了但像素值不变时 VMAF 照样打 99 分，播放器却会把 BT.2020/PQ 的片子按 SDR 显示，画面发灰——那不是"视觉无损"。源本身没标的项不作要求（编码器补默认值不算退化）。编码时也会把源的四项色彩属性显式写给编码器；新版 ffmpeg 本来就会自动传递，显式写是给老构建兜底。

4K 片源会自动换用 `vmaf_4k_v0.6.1` 模型（`vmaf_4k_model = false` 可关掉）。默认模型是按「1080p 屏幕、3 倍屏高」的观看条件训练的，用它量 4K 就是量错了尺子；4K 模型对应「4K 屏幕、1.5 倍屏高」，同样的失真下分数更高（实测同一对文件 95.0 → 97.4），因为那个观看条件下确实更不容易看出来。1440p 这类中间分辨率仍用默认模型。构建里没有 4K 模型时会退回默认模型并打警告——不退回的话 libvmaf 直接报错，会让 4K 文件的画质门槛被整个跳过。自适应搜索和这里的抽检共用这一个开关，两边必须是同一把尺子。

VMAF 抽检是逐帧比对，参考段和待测段错一帧分数就会崩到 30~60 分（画质其实没问题）。为此抽样切点会自动挪到两帧中间：同一帧的时间戳在 mp4（1/24000）和 mkv（1/1000）里精度不同，切点压在帧边界上时两边会一边留、一边丢，整段就错开了。万一还是错了帧（VFR 等异常片源），前后各挪一帧复核，分数能回到正常水平就按对齐后的分数算，并在日志里说明。失败信息会带上每段的位置（如 `53.05@4490s`）方便自己去那个时间点看一眼。

`keep_failed_output = true` 可以在校验没过时把产物留成 `<源文件名>.failed.mkv` 而不是直接删掉——几小时的编码结果值得先自己看一眼再决定。原文件始终不动；这个名字已被占用时不覆盖，仍然丢弃产物。被中断（Ctrl+C）时不会留 `.failed`，那不是校验失败。

`--keep` 也可在配置中设为默认：在 `[run]` 节加 `keep_original = true`。

## 日志与记录

- `logs/vcompress.log`：完整日志（DEBUG 级，含每次 VMAF 探测值），按 `log_max_bytes`（默认 5 MB）轮转，保留 `log_backups`（默认 3）份
- `logs/results.jsonl`：每个文件一行 JSON 的处理记录，用于断点续跑和统计

`[run]` 里这两个路径写相对路径时，按 **配置文件所在目录** 解析（不是当前工作目录），所以从任何目录运行都读写同一份记录；写绝对路径或 `~/...` 也支持。没有配置文件时（打包安装且未提供 `config.toml`）按当前工作目录解析。

每次运行结束会打印本次和累计省下的空间。

## 项目结构

```
video-compress/
├── config.toml          # 配置覆盖层（可选，默认值在 config.py 里）
├── pyproject.toml       # 打包与 vcompress 命令入口
├── vcompress/
│   ├── __main__.py      # 入口（python -m vcompress）
│   ├── cli.py           # 参数解析、构建自检、调度与汇总
│   ├── config.py        # 内置默认值、深合并、配置校验
│   ├── pipeline.py      # 单文件流程（探测→编码→校验→落地）与运行上下文
│   ├── probe.py         # ffprobe 探测与跳过判定
│   ├── encode.py        # 编码参数生成与执行、抽样、自适应 CRF、像素格式适配
│   ├── verify.py        # 解码校验、基础比对、VMAF 抽检与校验链编排
│   ├── files.py         # 文件发现、命名规则、输出名预留、删除策略
│   ├── results.py       # results.jsonl 读写与缓存新鲜度判断
│   ├── progress.py      # 单行进度渲染
│   ├── proc.py          # 子进程注册表与取消（Ctrl+C 时统一杀干净）
│   └── util.py          # 类型宽松转换与格式化
└── tests/               # 测试（unittest）
    ├── helpers.py           # 配置与 VideoInfo 替身
    ├── test_config.py       # 默认值/配置文件一致性与校验行为
    ├── test_probe.py        # 码率估算与跳过判定
    ├── test_encode.py       # 参数生成、进度解析、像素格式、构建能力
    ├── test_verify.py       # 校验链、VMAF 判定
    ├── test_files.py        # 命名、扫描、输出名并发预留
    ├── test_results.py      # 记录存储与缓存新鲜度
    ├── test_pipeline.py     # 流程编排：中断/失败清理、落地行为
    ├── test_cli.py          # 参数解析、自检、日志轮转、调度选择
    └── test_integration.py  # 真实 ffmpeg 端到端（缺 ffmpeg 自动跳过）
```

## 开发与测试

```bash
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

**Q：启动就报 `unknown option [skip] xxx` 怎么办？**
配置里有代码不认识的键，通常是拼写错误。报错信息里的键名就是问题所在；确认拼写或删掉该行即可。
