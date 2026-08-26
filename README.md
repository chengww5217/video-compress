# video-compress

基于 ffmpeg 的批量视频压缩工具。目标：**视觉无损**前提下尽可能压缩体积，压缩校验通过后才删除原文件，并通过元数据标记与码率阈值防止重复压缩。

**边界：只换编码，不动画面**——不裁黑边、不缩放、不改帧率、不修片源。唯一画面级改动是位深提到 10-bit 和必要时的色度降级，详情见 [架构说明](docs/architecture.md#边界只换编码不动画面)。

## 快速开始

```powershell
# 1. 先预览（不做任何修改，显示哪些会被压缩/跳过）
python -m vcompress D:\videos --dry-run

# 2. 正式压缩整个目录（递归），默认保留原文件、产出 a.av1.mkv 这类副本
python -m vcompress D:\videos

# 3. 压缩校验通过后删除原文件
python -m vcompress D:\videos --delete-original
```

也可以装成命令用：

```bash
pip install .              # 或 pip install ".[recycle]"
vcompress ~/videos --dry-run
```

> 环境要求（Python ≥ 3.11 + ffmpeg，建议带 libvmaf）、安装 ffmpeg、完整命令行参数与退出码见 [使用指南](docs/usage.md)。

## 常用参数速览

| 参数 | 作用 |
|---|---|
| `--dry-run` | 只预览，不改任何文件 |
| `-o, --output 目录` | 输出到指定目录（默认源文件所在目录） |
| `--delete-original` | 压缩校验通过后删除原文件（默认保留） |
| `--keep` | 明确保留原文件（默认行为） |
| `--encoder x265` | 换编码器（播放兼容性更好，体积略大） |
| `--workers N` | 并发处理多个文件 |
| `--recycle` | 删除原文件时进回收站 |
| `--force` | 忽略缓存重新处理 |

## 配置

所有默认值内置在代码里，`config.toml` 只是覆盖层。写错键名或取值越界会直接报错并指出问题。详见 [配置说明](docs/configuration.md)。

主要旋钮：

- **`[encode].encoder`**：`av1`（默认，体积最小）/ `x265`（兼容性更好）
- **`[adaptive].target_vmaf`**（默认 95）：视觉无损基准，降到 92~93 体积更小
- **`[adaptive].enabled`**：自适应 CRF 搜索（默认开），关闭则用固定 CRF
- **`[run].delete_original`**：默认 `false`（保留原文件），设 `true` 则默认删除原文件
- **`[run].shutdown_when_done`**：默认 `false`；设 `true` 则所有任务结束后自动关机。压缩过程中改也生效（结束时才重读配置），关机前会自动写回 `false`，属于一次性开关。详见 [完成后自动关机](docs/configuration.md#完成后自动关机-shutdown_when_done)

## 设计要点

- 自适应 CRF 搜索：多段抽样 + VMAF 二分，0.25 级粒度，取最差段成绩
- 校验链：解码 / 时长 / 分辨率 / 流数量 / 体积 / 色彩与 HDR / VMAF 抽检，任何一项不过都保留原文件
- 防重复压缩三道保险：元数据标记 + 每像素码率阈值 + 收益门槛
- 日志中文、控制台带颜色高亮重点，文件日志每次启动打一条 `==== 启动程序 ====` 分隔线，详见 [日志与记录](docs/usage.md#日志与记录)
- 压缩慢是设计取向：默认极限压缩档（preset 2/placebo），一部电影可能需要数小时

详见 [架构与工作原理](docs/architecture.md)。

## 文档导航

| 文档 | 内容 |
|---|---|
| [docs/usage.md](docs/usage.md) | 环境要求、安装 ffmpeg、快速开始、命令行参数、输出命名、日志与记录 |
| [docs/configuration.md](docs/configuration.md) | config.toml 全量配置说明 |
| [docs/architecture.md](docs/architecture.md) | 边界、工作流程、自适应搜索与校验原理、项目结构 |
| [docs/development.md](docs/development.md) | 开发与测试 |
| [docs/limitations.md](docs/limitations.md) | 已知限制与常见问题 |

## 项目结构

```
video-compress/
├── config.toml          # 配置覆盖层（可选，默认值在 config.py 里）
├── vcompress/           # 源码（入口、CLI、配置、流水线、编码、校验等）
├── tests/               # unittest 测试（含真实 ffmpeg 端到端）
└── docs/                # 文档
```

模块结构与依赖关系见 [开发与测试](docs/development.md) 和 [项目结构](docs/architecture.md#项目结构)。
