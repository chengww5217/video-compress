# 开发与测试

## 运行全部测试

```bash
# 单元测试无需 ffmpeg；集成测试缺 ffmpeg 时自动跳过
python -m unittest discover -s tests -v

# 只跑集成测试
python -m unittest tests.test_integration -v
```

## 模块职责

| 模块 | 职责 |
|---|---|
| `__main__.py` | 入口（`python -m vcompress`） |
| `cli.py` | 参数解析、构建自检、调度与汇总 |
| `config.py` | 内置默认值、深合并、配置校验 |
| `pipeline.py` | 单文件流程（探测→编码→校验→落地）与运行上下文 |
| `probe.py` | ffprobe 探测与跳过判定 |
| `caps.py` | 这个 ffmpeg 构建能干什么 + 像素格式决策 |
| `tracks.py` | 音轨/字幕处置与按此预估体积 |
| `encode.py` | 编码参数生成与编码执行（含进度解析） |
| `adaptive.py` | 自适应 CRF 搜索（0.25 级二分） |
| `vmaf.py` | 抽无损样本 + libvmaf 打分（搜索与校验共用） |
| `verify.py` | 解码校验、基础比对、VMAF 抽检与校验链编排 |
| `files.py` | 文件发现、命名规则、输出名预留、删除策略 |
| `results.py` | results.jsonl 读写与缓存新鲜度判断 |
| `progress.py` | 单行进度渲染 |
| `proc.py` | 子进程注册表、取消守卫（取消后拒绝再起进程）、从 ffmpeg stderr 里提取失败原因 |
| `scratch.py` | 临时工作目录：无损样本片段放哪里（默认不放系统临时目录，那多半是内存盘） |
| `power.py` | 完成后自动关机（结束时重读配置、写回 false、下发关机命令） |
| `logfmt.py` | 日志呈现：控制台上色与重点高亮、文件日志的会话分隔 |
| `util.py` | 类型宽松转换与格式化 |

测试目录 `tests/` 一个模块对应一份测试。集成测试覆盖真实 ffmpeg 端到端（缺 ffmpeg 自动跳过）。

模块之间的依赖是单向的：`caps`/`tracks`/`vmaf` 只依赖 `proc`/`util`，`encode` 用 `tracks` 的容器分类，`adaptive` 用 `encode`+`vmaf`，`verify` 用 `vmaf`。校验模块不再为了量 VMAF 去依赖编码模块。
