# 使用指南

## 环境要求

| 组件 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Windows / macOS / Linux | 主要在 Windows 与 macOS 上使用 |
| Python | ≥ 3.11 | 仅用标准库（需内置 `tomllib`） |
| ffmpeg / ffprobe | 需含 `libsvtav1`、`libx265` | **强烈建议带 `libvmaf`**：整套策略（自适应 CRF、画质闸门）都建立在它上面，没有它就只能用固定 CRF 且校验少一道。`libx264` 用来抽无损样本片段，没有会退回 ffv1，只是临时文件更大 |
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

# 4. 压缩后删除原文件（默认是保留原文件）
python -m vcompress D:\videos --delete-original
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
| `--keep` | 只生成压缩副本、不删原文件（**默认行为**） |
| `--delete-original` | 压缩校验通过后删除原文件（与 `--keep` 互斥，覆盖默认的保留行为） |
| `--config 路径` | 指定配置文件（默认同目录下 `config.toml`；显式指定但文件不存在会报错退出） |

退出码：`0` 全部成功；`1` 存在失败；`2` 配置或环境有问题（没跑任何编码）；`130` 被 Ctrl+C 中断。

中断是安全的：Ctrl+C 会立刻终止所有 ffmpeg 子进程、删掉半成品临时文件，原文件不受影响。串行和并发两种模式都一样。漏下的临时文件会在下次处理同一个文件时清掉。

## 完成后自动关机

挂着跑一晚上、跑完自己关机：把配置文件里 `[run] shutdown_when_done` 改成 `true` 即可。**没有对应的命令行参数**，每次想关机都要手动改这一行。

```toml
[run]
shutdown_when_done = true
shutdown_delay_seconds = 60   # 关机前的倒计时，期间 Ctrl+C 可取消
```

- 任务**开始**时控制台会播报当前设置（`auto shutdown: ON/OFF`）；关着的时候会顺带提示怎么打开
- 开关是在**所有任务结束后**重新读一次配置文件才生效的，所以压缩已经跑起来了再改主意也来得及：直接改配置存盘，不用中断重跑
- 关机前脚本会把 `shutdown_when_done` 写回 `false`——一次性开关，下次想关机得再手动打开；写不回去就放弃关机（避免留下每次运行都关机的隐患）
- `--dry-run`、Ctrl+C 中断、倒计时期间再按 Ctrl+C，都不会关机；有文件压缩失败**不影响**关机
- 全过程写日志：重读结果、写回 `false`、实际下发的关机命令都在 `logs/vcompress.log` 里

规则的来龙去脉、各平台用的关机命令、macOS 的授权提示见 [配置说明](configuration.md#完成后自动关机-shutdown_when_done)。

## 输出命名与覆盖保护

输出容器由 `[encode].output_container` 决定（默认 `.mkv`）：

- 常规情况 `a.mp4` → `a.mkv`，校验通过后才删除 `a.mp4`；`a.mkv` → 原地替换
- 目标名已经被**另一个文件**占用时，输出改名为 `a.<编码器><扩展名>`（如 `a.av1.mkv`）并打一条警告，绝不覆盖已有文件
- 连改名后的名字也被占用，这个文件直接报错跳过
- 默认**保留原文件**，只产出 `a.av1.mkv` 这类副本；想要压缩后删除原文件，加 `--delete-original`（或把 `[run] delete_original = true`）

名字冲突在编码开始前就会判定，不会白跑几小时才失败。并发模式下输出名会在内存里预留：同目录的 `a.mp4` 和 `a.avi` 都想产出 `a.mkv` 时，第二个自动让位到 `a.av1.mkv`，两个 worker 不会抢同一个名字。

临时文件名是 `<源文件完整名>.tmp<扩展名>`（如 `a.mp4.tmp.mkv`）。带上源文件的扩展名是必要的：只用主干名的话 `a.mp4` 和 `a.avi` 会共用一个 `a.tmp.mkv`，并发时两个 ffmpeg 往同一个文件写。

## 日志与记录

- `logs/vcompress.log`：完整日志（DEBUG 级，含每次 VMAF 探测值），按 `log_max_bytes`（默认 5 MB）轮转，保留 `log_backups`（默认 3）份
- `logs/results.jsonl`：每个文件一行 JSON 的处理记录，用于断点续跑和统计

`results.jsonl` 是追加写的，同一个文件反复重跑（`--force`）会攒下多条记录。启动时如果发现一半以上的行都已经被后来的记录顶掉、且总行数超过 1000，会自动压实成"每个路径一条"：先写临时文件再原子替换，压实失败或中途中断时磁盘上仍是原来那份。一万个不同文件的一万行不算冗余，不会被动。

`[run]` 里这两个路径写相对路径时，按 **配置文件所在目录** 解析（不是当前工作目录），所以从任何目录运行都读写同一份记录；写绝对路径或 `~/...` 也支持。没有配置文件时（打包安装且未提供 `config.toml`）按当前工作目录解析。

每次运行结束会打印本次和累计省下的空间。
