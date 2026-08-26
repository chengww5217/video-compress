# 配置说明（config.toml）

所有默认值都内置在代码里（`vcompress/config.py` 的 `DEFAULTS`），`config.toml` 只是覆盖层：

- 少写一个配置节不会出错，缺的部分用默认值补齐；
- **写错键名会直接报错并指出是哪一个**（比如把 `min_saving_ratio` 打成 `min_saving_ration`），不会被静默忽略；
- 取值范围也会校验（比例必须在 0~1、`crf_min < crf_max`、`workers ≥ 1` 等）。

## 编码器选择 `[encode]`

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

## 自适应 CRF `[adaptive]`

默认开启。对每个视频从整片均匀抽 `sample_count`(4) 段、每段 `sample_seconds`(10) 秒，用 VMAF 二分搜索「满足目标画质的最小码率」对应的 CRF：

- 一个 CRF 的成绩取**最差段**，不取平均：平均分能被好段抬起来，观感却是最差处决定的。某段已经不达标时后面的段直接不量，省时间
- 只看中点一段是不够的：那个位置可能正好是明亮的动作戏或全黑的过场，按它定的 CRF 拿到暗场就不成立了。铺开取样后，搜索和后面用多段抽检的校验才是同一个口径
- `target_vmaf = 95`：视觉无损基准。想更激进可降到 92~93（体积更小、画质略降）
- `target_margin = 0.05`：搜索余量。样本最差段要过 `target_vmaf + target_margin` 才算达标——贴线（95.02 vs 95.0）的 CRF 会被放弃，自动降一档。几段十来秒的样本外推到一两小时的全片有 ±0.5 分的波动很正常，零余量等于把宝全押在抽样运气上。写 0 关闭
- `crf_min/crf_max`(16~50)：搜索边界。上界给到 50 是因为闸门是画质而不是 CRF：动画之类的简单片源真能在 CRF 45+ 还稳稳过 95 分，卡在 44 就是白白留着体积不省
- `search_step = 0.25`：**搜索按 0.25 级 CRF 走，不是整数**。本机实测 1 级 CRF ≈ 8% 体积、0.25 级 ≈ 2%，只搜整数等于平均白扔半级 ≈ 4% 的体积，而画质闸门一点没变。小数 CRF 的下发方式各家不同：libx265 的 `-crf` 本身是浮点，libsvtav1 的 `-crf` 只收整数、得走 `-svtav1-params crf=`（会覆盖前者，整数那个留着给忽略这个参数的老构建兜底）；不认识的编码器取整

## 跳过规则 `[skip]`

- `bpp` 表：各编码器的每像素码率下限，低于即视为已压到位（只按视频轨码率算）
- `expected_ratio` 表：各编码器的预估压缩比，据此估算收益
- `min_saving_ratio = 0.15`：预估收益低于此值不压
- `min_size_mb = 20`：小文件跳过
- `skip_interlaced = true`：交错源（`field_order` 是 `tt/bb/tb/bt`）直接跳过，理由见「已知限制」
- `extensions`：处理的容器白名单。默认不含 `.ts`——它和 TypeScript 源码撞扩展名，扫到代码目录会把文本文件当 Transport Stream 收进来；真压 TS 流把 `".ts"` 加回列表即可
- `tag_marker = "vcompress"`：写入和识别用的元数据标记名，两边始终一致

## 音频与其它轨道 `[audio]`

逐条音轨判断，不会因为一条无损轨就把整个文件的音轨全部重压：

| 音轨 | 处理 |
|---|---|
| 普通有损轨（AAC/AC3/DTS…，码率 ≤ `transcode_over_kbps`） | 直接复制 |
| 码率超过 `transcode_over_kbps`(512k) 的有损轨 | 转 Opus |
| 无损轨（PCM/ALAC/TrueHD/MLP…） | 转 **FLAC**，仍是数学无损 |
| 已经是 FLAC 的轨 | 直接复制，不重编 |

无损轨默认转 FLAC 而不是 Opus，理由是这个工具压完要**删原文件**：把 TrueHD/PCM 换成有损轨就再也换不回来了，而 FLAC 是数学无损、PCM 转过去还能省三到四成。想要更小的体积就设 `[audio] lossless = "opus"`（5.1 的 TrueHD 从约 3 Mbps 降到 480k，一部电影能再省一两个 GB，代价是这一步不可逆）。输出容器是 mp4 系时自动改用 Opus——FLAC 在 mp4 里播放器支持很差。

Opus 码率以 `opus_kbps`(160k) 作为**立体声**基准按声道数缩放：单声道 80k、5.1 得 480k、7.1 封顶 510k。

字幕、章节、附件都会保留。文本字幕按目标容器自动转换（进 mkv 转 `srt`，进 mp4 转 `mov_text`），图形字幕（PGS/VobSub）原样复制。matroska 装不下的 data 流（如 mp4/mov 里的 tmcd 时间码轨）会被丢弃——否则整个转码会直接失败。

## 校验 `[verify]`

| 检查项 | 默认 | 失败处理 |
|---|---|---|
| 全量解码无报错 | 开 | 放弃输出 |
| 时长偏差 ≤ 0.5s | 是 | 放弃输出 |
| 分辨率与原文件一致 | 是 | 放弃输出 |
| 音轨、字幕条数与原文件一致 | 是 | 放弃输出 |
| 输出必须更小且省 ≥ 5% | 是 | 保留原件 |
| 色彩属性与 HDR 母版信息一致 | 开 | 保留原件 |
| VMAF 抽检 5 段均值 ≥ 93 | 开 | 保留原件 |
| 且每一段都 ≥ 90 | 开 | 保留原件 |

检查按「先便宜后昂贵」的顺序跑：解码、时长、体积、色彩元数据都过了才会去做最贵的 VMAF 抽检。任何一项不过都会删除临时文件并保留原文件，不会出现"压坏了还删了原件"的情况。ffmpeg 构建里没有 libvmaf 时抽检会跳过并打警告，其余检查照常执行。

跑过自适应搜索的产物还会按 `target_vmaf` 复核一次抽检结果：搜索和校验的抽样位置不同，搜索达标不代表全片达标。均值或最低段低于目标时产物仍按通过保留（它过了上面这些闸门），但会打一条 WARNING 留痕，介意可以删掉产物加 `--force` 重压。

VMAF 抽检的判定细节、色彩/HDR 比对原理见 [architecture.md](architecture.md#校验)，`keep_failed_output` 见下。

`color_check`（默认开）比对 `color_primaries`/`color_transfer`/`color_space`/`color_range` 四项，以及 HDR10 母版信息（mastering display / content light level）、HDR10+、Dolby Vision RPU 是否还在。这项不能交给 VMAF：元数据丢了但像素值不变时 VMAF 照样打 99 分，播放器却会把 BT.2020/PQ 的片子按 SDR 显示，画面发灰——那不是"视觉无损"。源本身没标的项不作要求（编码器补默认值不算退化）。编码时也会把源的四项色彩属性显式写给编码器；新版 ffmpeg 本来就会自动传递，显式写是给老构建兜底。

`keep_failed_output = true` 可以在校验没过时把产物留成 `<源文件名>.failed.mkv` 而不是直接删掉——几小时的编码结果值得先自己看一眼再决定。原文件始终不动；这个名字已被占用时不覆盖，仍然丢弃产物。被中断（Ctrl+C）时不会留 `.failed`，那不是校验失败。

## 运行行为 `[run]`

| 键 | 默认 | 说明 |
|---|---|---|
| `workers` | 1 | 并发处理的文件数 |
| `results_file` | `logs/results.jsonl` | 处理记录（相对路径按配置文件所在目录解析） |
| `log_file` | `logs/vcompress.log` | 日志文件（实际按日期分卷，写成 `vcompress-2026-08-26.log` 这样） |
| `log_max_bytes` | 5242880 | 日志轮转阈值 |
| `log_backups` | 3 | 保留的日志备份份数 |
| `use_recycle_bin` | false | 删除原文件时进回收站 |
| `delete_original` | false | 压缩校验通过后删除原文件（等价于每次都加 `--delete-original`） |
| `shutdown_when_done` | false | 全部任务结束后自动关机（一次性开关，关机前会被写回 false） |
| `shutdown_delay_seconds` | 60 | 下发关机命令前的倒计时秒数，期间 Ctrl+C 可取消；0 = 立即关 |

默认保留原文件，想改成默认删除原文件就在 `[run]` 节设 `delete_original = true`，或每次都加 `--delete-original`。

### 完成后自动关机 `shutdown_when_done`

压一整个目录动辄几个小时，这个开关是为「晚上挂着跑、跑完自己关机」准备的。

```toml
[run]
shutdown_when_done = true
shutdown_delay_seconds = 60
```

三条刻意的规则：

1. **只认配置文件，没有命令行参数。** 想让某次运行结束后关机，就得手动把这一行改成 `true`——关机这件事不该被一个顺手敲上的参数触发。
2. **结束时才重新读一次配置文件。** 任务已经跑起来了再改主意（想关 / 不想关）都来得及，改完存盘即可，不用中断重跑。开关是在所有文件都处理完、汇总打印完之后读的，读的是**磁盘上当时的内容**，不是启动时那份。
3. **关机前先把配置写回 `false`。** 这是一次性开关：下次想关机必须再手动打开一次，不会因为忘了改配置而在下次运行结束时莫名其妙关机。写回失败（文件只读、找不到那一行等）时**放弃关机**并报错——宁可不关，也不留一个每次都关机的隐患。

任务开始时控制台会先播报当前设置，关掉的时候顺便告诉你怎么打开：

```
auto shutdown: OFF — set [run] shutdown_when_done = true in /path/config.toml to power off 60s after all tasks finish; you can flip it while the run is in progress ...
```

打开时是：

```
auto shutdown: ON — the machine powers off 60s after all tasks finish; the flag is read again at the end ...
```

结束阶段的每一步都写进日志（`re-read <config>: [run] shutdown_when_done = true` → `reset [run] shutdown_when_done = false` → `running: <关机命令>`），事后能从 `logs/vcompress.log` 里看清是关了、没关、还是被取消。

不关机的情形：`--dry-run`、被 Ctrl+C 中断、倒计时期间再按 Ctrl+C、配置文件不存在（打包安装且没有 `config.toml` 时没有可翻的开关）、配置文件坏掉读不出来。**有文件压缩失败不影响关机**——挂机跑的目的就是跑完就关，失败情况看日志和 `results.jsonl`。

关机命令按平台选，不需要 sudo：

| 平台 | 命令 |
|---|---|
| Windows | `shutdown /s /t 0` |
| macOS | `osascript -e 'tell application "System Events" to shut down'` |
| Linux | `systemctl poweroff`（没有 systemctl 时 `shutdown -h now`） |

macOS 首次执行可能弹窗要求授权终端控制「系统事件」，拒绝授权会导致关机失败（日志里能看到 osascript 的报错）。Linux 上 `shutdown -h now` 这条路径通常需要相应权限。
