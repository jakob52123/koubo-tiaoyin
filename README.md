# koubo-tiaoyin · 口播调音

给已合成的 AI 克隆声 / TTS 做离线后期：去首尾静音留边、按比例压段内停顿、有限段级电平匹配、拼接和两遍响度。默认不加音色效果、不降噪。

运行只需 **Python 3.9+ 标准库 + PATH 中的 ffmpeg / ffprobe**，支持 macOS 自带 `/usr/bin/python3`。不安装 Python 音频库、不联网、不调用 MiniMax、不重新扣费。

## 特点

- **按比例压停顿，不是一刀切。** 新停顿 = 0.16 + K ×（原停顿 − 0.16），最长 0.42 秒。原来长的停顿压完还是比短的长，说话的节奏层次还在；常见做法是删光静音或统一留白，会把语气压平。
- **先压段内，再拼段。** 段落之间的停顿单独留白，不会和句间停顿一起被压掉。
- **专为克隆声 / TTS 设计。** 合成声本来就干净，所以默认不降噪、不改音高、不强压缩；EQ、去齿音、轻压缩、底噪都是听完觉得有问题再打开的开关。
- **同响度对比。** 每次都另出一份「对比原样」，响度和成品一样，避免把「更响」误听成「更好」。
- **字幕不会错位。** 剪掉停顿后时间轴会变，附带一张 timemap 用来重新对齐字幕。
- **参数都对照 FFmpeg 文档核过。** 网上常见的错误参数（例如把 `deesser` 的 f 写成 4000Hz、`speechnorm` 的 m 写成 10）都没有用；真峰目标留了余量。
- **安全。** 永远不覆盖原文件；中途失败不留半成品；复测不达标时退出码是 2，不会冒充合格。

## 用法

```bash
/usr/bin/python3 scripts/tiaoyin.py check 01.wav 02.wav
/usr/bin/python3 scripts/tiaoyin.py polish 01.wav 02.wav --out 成品.wav
# 太赶就放松压停顿；新文件名保留上次结果
/usr/bin/python3 scripts/tiaoyin.py polish 01.wav 02.wav --k 0.4 --cap 0.55 --out 成品-舒缓.wav
# 仅在听后确认问题时开需要的模块
/usr/bin/python3 scripts/tiaoyin.py polish 01.wav 02.wav --eq clear --deess --comp light --out 成品-轻处理.wav
/usr/bin/python3 scripts/tiaoyin.py polish 01.wav 02.wav --room 有授权的房间底音.wav --out 成品-底音.wav
```

输入顺序就是段落顺序。输出目录须已存在。输入和底噪永远只读，包括输出的硬链接/软链接别名都不能覆盖输入。已有成品或任一附带文件默认拒绝覆盖；用户确认后可用 `--force` 替换旧交付，但仍不能覆盖输入。输出软链接和非普通文件也拒绝。

先 check → 默认 polish → 用户听成品和「对比原样」→ 按反馈从原始文件重新跑。不要在成品上继续叠效果，也不要自动打开播放器。

## 参数

`check <wav...>` 只读打印时长、原音轨的 loudnorm `input_i / input_lra / input_tp`、内部停顿处数/中位/最长，多段时打印最大−最小响度差，最后给建议。多声道文件的 check 响度是原声道布局；polish 明确先下混单声道，两者计量对象不同。

`polish <wav...> --out <成品.wav>`：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--out` | 必填 | WAV 成品，不允许与任一输入/底噪/附带文件冲突 |
| `--k` | 0.25 | 0–1；越大越舒缓；仍受 cap 限制 |
| `--cap` | 0.42 秒 | 内部停顿公式上限，≥0.16；调大才能保留更长气口 |
| `--keep-pauses` | 关 | 禁用段内压停顿；仍会去首尾静音和拼接 |
| `--gap` | 0.5 秒 | 段与段之间新增静音，≥0；实际气口另含各段尾/头余量 |
| `--no-level-match` | 关 | 禁用多段电平匹配 |
| `--target` | -16 LUFS | loudnorm I 目标，-70 至 -5；是工程目标，不是平台官方标准 |
| `--eq clear` | 关 | 高通80Hz，300Hz减2dB，3kHz加2dB |
| `--deess` | 关 | 轻量去齿音，无量纲参数 i=0.2 / m=0.3 / f=0.5 |
| `--comp light` | 关 | threshold=-20dB / ratio=2 / attack=10ms / release=150ms |
| `--room 文件` | 关 | 底噪下混单声道，循环铺满，响度归一前调到 -52 dBFS RMS；须有素材授权 |
| `--force` | 关 | 允许替换旧成品及附带文件，不能改输入 |

数值参数必须有限。完整帮助：`/usr/bin/python3 scripts/tiaoyin.py polish --help`。

## 固定处理顺序

1. **解码**：ffmpeg 首个音轨 → 48kHz 单声道 float32（f32le），立体声下混。检查空文件、无音轨、非有限采样、下混后全静音。
2. **每段去首尾静音留边**：出声点为 `abs(sample) > 本段峰值 × 10^(-50/20)`，字头留30ms、字尾留120ms（不超过源边界），沿用作者做口播视频时验证过的做法。
3. **每段按比例压停顿**：10ms帧算 RMS dB，3帧滑动平均（与原算法相同的零填充边界），阈值为平滑 dB 的 P90−35；只处理不碰段首尾、≥0.16秒的静音帧段。目标长度 `min(0.16 + K×(原长−0.16), cap)`，从中间删除，10ms线性交叉淡化；删除区不足两倍 fade 则不剪。`--keep-pauses` 跳过。
4. **多段电平匹配**：测压停顿后各段 I，以中位数为参考，偏差>1.5LU 才向中位数调，最多±3dB，不逐轮追平；每段调整写进报告。单段不匹配。
5. **拼接**：整条头0.15秒，段间 gap，尾0.6秒；每段首尾10ms线性淡入淡出。段间留白不进入步骤3。源余量充足时，段间实际气口约 gap+0.03+0.12秒。
6. **整条可选处理**（默认全关），按 EQ → 去齿 → 轻压 → 底噪顺序：
   - EQ：`highpass=f=80,equalizer=f=300:t=q:w=1:g=-2,equalizer=f=3000:t=q:w=1:g=2`
   - 去齿：`deesser=i=0.2:m=0.3:f=0.5`（f 不是 Hz）
   - 轻压：`acompressor=threshold=-20dB:ratio=2:attack=10:release=150`
   - 底噪：用 `astats` 测解码后 RMS，增益为 `-52 - 测得RMS` dB；循环、`amix normalize=0` 混在人声下，长度跟人声。-52 是最终 loudnorm **之前**的底噪电平，最终会跟随整体增益改变。
7. **两遍响度**：对同一个预母版，第一遍 `loudnorm=I=<target>:TP=-2.0:LRA=11:print_format=json`；第二遍传入全部 measured_* 和 offset，要求 `linear=true`，实际可能 dynamic。日志中的 `normalization_type` 如实进报告，不谎称一定线性。输出 48kHz / mono / pcm_s24le WAV。
8. **复测、对比和发布**：最终 WAV 再测 input_* 和内部停顿；原输入仅统一解码格式后首尾相接，独立做相同目标的两遍响度，生成同响度对比。所有处理完成后才发布文件；工具/输入错误清理临时文件，不留下本次半成品。发布中普通文件系统错误有回滚，不能承诺断电级跨文件事务。

### 输出文件和退出码

以 `成品.wav` 为例：

- `成品.wav`：处理后的 PCM24 成品。
- `成品.report.json`：每步参数、源格式、各段处理前/裁边后/压缩后时长、裁切点、保留区间、电平调整、前后停顿与响度、成品格式、归一两遍日志/实际模式、底噪 RMS 和增益、ffmpeg 版本、复测结果。全局「前」是原始段落直接相接，「后」是最终成品；各段另有独立统计。
- `成品.timemap.tsv`：`源文件\t源秒\t成品秒`，每个保留片段起点一行（源秒相对各自原文件），计入裁边、删停顿、交叉淡化重叠、头部和 gap。成品秒严格递增，最后起点早于成品结尾。report 的保留区间采样坐标相对裁边后的本段，输出起点相对压缩后的本段。10ms交叉淡化区同时存在两个来源，映射不代表语义逐字精度。
- `成品-对比原样.wav`：不裁边、不压停顿、不加 gap、不匹配电平、不加效果，仅格式统一并做两遍响度，不是输入的字节拷贝。

退出码 **0**：成品和对比均在 target±1 LUFS，真峰≤-1.5dBTP；**2**：复测未达标，文件与报告仍保留供诊断，不算合格交付；**3**：输入、参数或工具错误，中文报错，不发布本次半成品，已有文件保持原样。终端 polish 摘要不超过6行。数值达标不等于听感满意。

## 统计口径与局限

- 诊断/报告统一 `silencedetect=noise=-40dB:d=0.15`，通过坐标排除首尾，而非盲目删首尾两项。压停顿算法用相对 P90 阈值，与这套绝对阈值统计**不是同一个检测器**。
- 0.20/0.40/0.80/1.20秒理论映射为0.17/0.22/0.32/0.42秒；帧边界、平滑、10ms重叠会让实测略有差别，短停顿可能跌到150ms统计门限以下，不意味着删掉整处停顿。
- 不降噪、不改音高、不加速发音、不做语义判断、不识别口误。会压掉有意的情绪停顿；需要保留时调大 K/cap 或 `--keep-pauses`。没有逐处语义保护开关。
- 单声道下混可能相消；下混全静音会拒绝，部分相消需人工检查。极短/极低电平音频可能无法得到有限 LUFS，同样拒绝，不编造零值。
- 底噪应为平稳、可循环的 room tone；本版直接循环，不自动修复底噪素材的循环接缝。不要用带人声、节奏、突发声或首尾突变的素材。底噪不是“去 AI 感”的保证。
- LRA=11 是响度滤镜参数，不要求实测 LRA 达到11。若动态/峰值约束无法同时满足目标，复测会报不合格，不自动换目标或偷偷重压。
- 浮点音频会驻留内存，适合数分钟口播，不是长节目流式剪辑器；每次工具调用有240秒超时。需 ffmpeg 构建带上述滤镜。不同版本可能有轻微数值差异。
- 改时长后必须重新对齐字幕；若以后加音乐或编码 AAC，须对**最终混音/编码文件**再测，不能沿用此裸口播的计量结果。

## 离线测试

运行环境本身不依赖 pytest；只有开发测试使用已有 pytest，不自动安装任何东西：

```bash
python3 -m pytest scripts/test_tiaoyin.py -q
```

测试在 `tmp_path` 中以 ffmpeg lavfi 现造音频，覆盖比例数学、保留停顿、段间隙、电平上限、成品计量/格式、输入哈希、安全拒绝与回滚、坏输入、立体声、各可选效果、映射/对比、只读诊断和错误码。数学测试用50ms检测下限观测很短的实际停顿，产品 check/report 仍固定150ms口径。

## 致谢与来源

**代码为本仓库原创，未复制第三方代码**。比例法和去首尾静音留边改写自作者做口播视频时写的压停顿、拼接脚本，只用标准库重新实现；不是把 PyDub 的固定留白移植过来。第三方只借方法论、测量字段关系及试听/工程思路，命令参数以 FFmpeg 文档（`ffmpeg -h filter=<名>`）为准自行组织，未运行来源仓库脚本。

下列仓库名和许可证依据 2026-10 的调研快照，不代表在线复核或整仓审核；致谢不是复制授权。许可证不明者仅列思路，不引入实现。

| 仓库 | 许可证（调研快照） | 借鉴思路 |
|---|---|---|
| [agricidaniel/claude-video](https://github.com/agricidaniel/claude-video) | MIT | 两遍响度和测量字段关系 |
| [josiahsiegel/claude-plugin-marketplace](https://github.com/josiahsiegel/claude-plugin-marketplace) | MIT | 两遍 measured_*、显式采样率；不采纳调研指出的错误配方 |
| [guia-matthieu/clawfu-skills](https://github.com/guia-matthieu/clawfu-skills) | MIT | 静音候选/留边、按需轻压、环境底层 |
| [calesthio/generative-media-skills](https://github.com/calesthio/generative-media-skills) | MIT | 规范与经验目标分开、输入/JSON防御、0/2/3错误码、最终复测 |
| [langchain-ai/design-agent](https://github.com/langchain-ai/design-agent) | Apache-2.0 | 按症状选择小模块、同响度旁路对比 |
| [browser-use/video-use](https://github.com/browser-use/video-use) | MIT | 切点余量、最终复测、人耳听审 |
| [curiositech/some_claude_skills](https://github.com/curiositech/some_claude_skills) | MIT | 克隆声上下游分工；不采用手写LUFS计量 |
| [glincker/ableton-skills](https://github.com/glincker/ableton-skills) | MIT | 区分风格，口播不照搬重音乐处理链 |
| [jperrello/C0BALT_CUT](https://github.com/jperrello/C0BALT_CUT) | 未给出 | 临时 staging 成功后发布 |
| [vivy-yi/xiaohongshu-skills](https://github.com/vivy-yi/xiaohongshu-skills) | 未给出 | 短接缝、手机试听、症状式轻EQ |
| [Jamailar/Beav](https://github.com/Jamailar/Beav) | NOASSERTION（未明确） | 句内与段间停顿分离、保留情绪气口 |
| [Vincentwei1021/video-talkcraft](https://github.com/Vincentwei1021/video-talkcraft) | NOASSERTION（未明确） | 先剪音频再对齐字幕、采样级偏移 |

未把 AGPL 或许可标识冲突来源的代码带入，也未把重复资料算成独立验证。若将来复制第三方实现，须另行核实许可证及 NOTICE、履行相应义务，不能只靠本表。

本仓库代码许可证：[MIT](LICENSE)，Copyright (c) 2026 Jakob。
