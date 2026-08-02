# wav2mc 基础项目

把 WAV 或 FFmpeg 支持的其他音频文件转换成 Minecraft Java 数据包，并通过一个可复用的混合颗粒资源包播放。

> 从安装到游戏内播放的完整步骤、参数解释与故障排查，请阅读 [详细使用指南](docs/usage.md)。

这个版本的目标是提供一个能运行、便于继续开发的基础架构，而不是高保真成品。它实现了：

- FFmpeg 解码、立体声保留/单声道化、重采样和带通滤波；
- 100 ms `sqrt-Hann` 正弦颗粒和 50 ms 帧移；
- 25 ms / 10 ms 多分辨率瞬态检测；
- 16 个感知频带的通用带限噪声与瞬态残差颗粒；
- 连续噪声变体跟踪和瞬态触发滞回；
- 带平滑恢复的逐帧残差峰值限制；
- STFT 幅度、相位分析；
- 按频段选取主要频率；
- 相邻帧峰值轨迹跟踪和频点切换迟滞；
- A-weighting 感知排序和 Bark 频带心理声学掩蔽；
- 可校准的 Minecraft 增益和非线性音量响应；
- 频率和相位量化；
- 本地 `preview.wav` 重建；
- 双声道预览和游戏内头部相对立体声定位；
- Minecraft 数据包 ZIP；
- 通用正弦、噪声和瞬态混合资源包 ZIP；
- 自适应频率网格和 `voice` / `normal` / `high` / `experimental` 四档资源包集；
- 可执行转换和资源包生成的 Tkinter 桌面 GUI；
- scoreboard 播放器和分层帧分发，避免长 `schedule` 链。

## 默认目标

默认参数按 Minecraft Java 26.2 设置：

- 资源包格式：88.0
- 数据包格式：107.1
- 现代目录：`data/<namespace>/function`
- 20 tick/s

格式号依据 [Minecraft Java Edition 26.2 官方发布说明](https://www.minecraft.net/en-us/article/minecraft-java-edition-26-2)。

其他版本请通过命令行覆盖 `--pack-format`、`--data-pack-format` 和 `--layout`。

## 环境

- Python 3.10+
- FFmpeg，且 `ffmpeg` 命令可在 PATH 中使用
- Tk 8.6+（仅 GUI 需要；部分 Linux 发行版需另装 `python3-tk`）
- Python 包：NumPy、SoundFile

## 输入格式

`convert` 接受本机 FFmpeg 能够解码的任意音频或媒体文件，例如 WAV、MP3、FLAC、M4A/AAC、OGG/Vorbis、Opus、AIFF，以及含音轨的 MP4/MKV。容器有多条音轨时可用 `--audio-stream` 选择，视频、字幕和数据流会被忽略。实际格式范围取决于已安装的 FFmpeg 构建。

立体声默认保留：左右声道独立分析，本地预览写成双声道 WAV，数据包把左右颗粒放在玩家朝向两侧 0.75 格。双单声道输入会自动折叠，避免无意义地重复命令。性能不足时使用 `--no-stereo` 强制下混为单声道；立体声不增加资源包文件，现有匹配档位的资源包可以继续使用。

## 安装

Windows PowerShell：

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

Linux/macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

检查：

```bash
wav2mc --help
```

## 图形界面

启动桌面 GUI：

```bash
wav2mc gui
```

也可以在启动时预选输入和输出目录：

```bash
wav2mc gui input.m4s --output-dir output
```

GUI 使用紧凑的双栏转换工作台。选择媒体后会异步显示时长、编码、采样率、声道和可用音轨；常用区提供四档模式与 -24～+12 dB 滑块，音轨、立体声和心理声学选项收在“高级设置”中。转换和资源包生成显示真实进度，主按钮可安全取消；所有正式输出只在任务完整成功后替换。

转换完成后，窗口内结果栏会显示时长、声道、预览峰值、实际命令数和资源包匹配状态，并可直接试听、打开目录或复制 `/function <namespace>:start`。资源包页可多选任意档位生成，并标记“有效 / 缺失 / 参数不匹配”。界面会自动记住目录、模式、增益和高级选项。重新执行 `pip install -e .` 后，也可直接运行 `wav2mc-gui`。

GUI 使用推荐模式预设，确保数据包和资源包 namespace、频率与相位一致。自定义频率网格、Minecraft 响度模型或旧版布局时仍使用 CLI。

也可以不安装为命令，直接在项目根目录运行：

```bash
python run.py --help
python run.py bank-build --output output/wav2mc_sine_bank.zip
python run.py convert input.wav --name demo_song
```

## 1. 先做一个小型资源包测试

这个命令只生成 100–500 Hz、4 相位的小型资源包，便于验证安装和声音路径：

```bash
wav2mc bank-build \
  --output output/wav2mc_test_bank.zip \
  --min-frequency 100 \
  --max-frequency 500 \
  --frequency-step 20 \
  --phases 4
```

## 2. 生成完整通用资源包

默认使用分段自适应频率网格，资源包参数必须和后续转换命令保持一致：

```bash
wav2mc bank-build \
  --output output/wav2mc_sine_bank.zip
```

| 频率范围 | 间隔 |
| --- | ---: |
| 20–1000 Hz | 20 Hz |
| 1000–4000 Hz | 40 Hz |
| 4000–8000 Hz | 80 Hz |
| 8000–12000 Hz | 160 Hz |
| 12000–20000 Hz | 320 Hz |

完整网格只有 225 个频点。边界频点只生成一次；档位截止频率不落在网格上时，以截止频率以下最后一个频点为准。

输出资源包中包含：

```text
assets/wav2mc/sounds/grain/f0440/p00.ogg
assets/wav2mc/sounds/grain/f0440/p01.ogg
assets/wav2mc/sounds/noise/b08/v00.ogg
assets/wav2mc/sounds/transient/b08/v00.ogg
...
assets/wav2mc/sounds.json
```

正弦和稳态噪声颗粒使用重叠窗，瞬态颗粒使用快速攻击、指数衰减包络。编码器不识别乐器，而是按频谱平坦度和频谱通量决定每个频带使用正弦、噪声或瞬态。

> 混合残差默认开启。升级后必须重新生成资源包，否则新数据包引用的 `noise.*` 和 `transient.*` 声音事件会缺失。文件名仍保留 `_sine_bank.zip` 以兼容现有脚本。

## 按设备性能生成资源包集

```bash
wav2mc bank-build-set --output-dir output/device_banks
```

| 模式 | 频率范围 | 正弦 | 噪声 | 瞬态 |
| --- | --- | ---: | ---: | ---: |
| `voice` | 80–8000 Hz | 12 | 2 | 2 |
| `normal` | 60–12000 Hz | 20 | 4 | 4 |
| `high` | 40–16000 Hz | 24 | 6 | 6 |
| `experimental` | 20–20000 Hz | 32 | 8 | 8 |

命令会生成四个独立 ZIP 和 `wav2mc-device-packs.json` 清单。转换时使用同一模式，会自动匹配频率、相位、质量和 namespace。`--mode` 是 `--device-profile` 的简写：

```bash
wav2mc convert input.wav --mode normal
```

对应资源包 namespace 为 `wav2mc_normal`。可用 `--profiles voice normal` 只生成部分模式。旧 `low` 模式仍可显式选择，但不在默认生成集合中。

## 3. 转换音频

```bash
wav2mc convert input.wav \
  --name demo_song \
  --quality normal \
  --bank-grain-level 1.0 \
  --output-dir output
```

输出：

```text
output/demo_song_datapack.zip
output/demo_song_preview.wav
output/demo_song_analysis.json
```

先听 `preview.wav`。本地预览已经使用相同的频率、相位、窗口和稀疏分量限制；如果预览本身失真严重，应先调整参数，而不是直接进入游戏测试。

立体声会接近翻倍每秒 `/playsound` 命令数。游戏内优先从 `--mode normal` 或 `--mode high` 开始；`experimental` 立体声只适合客户端和设备性能充足时使用。

## 响度校准

默认假设 Minecraft 的实际增益和 `/playsound` 音量都是线性的。如实测播放比预览安静，可写入实测模型：

```bash
wav2mc convert input.wav \
  --minecraft-gain 0.85 \
  --minecraft-volume-exponent 1.08 \
  --max-command-volume 1.0
```

`--minecraft-gain` 是命令音量为 1.0 时的“游戏内实测幅值 / 参考幅值”；指数描述命令音量的非线性响应。转换器会反解每个 `/playsound` 音量，并在超出可复现范围时同比缩放预览与数据包。分析报告会记录校准值、最大命令音量和预测误差。

## 心理声学掩蔽

掩蔽默认开启。分析器先用 A-weighting 计算感知显著度，再用非对称 Bark 扩散阈值剔除强峰附近不可闻的弱峰。较高的 `--masking-offset-db` 会保留更多分量；调试时可用 `--no-psychoacoustic-masking` 关闭。

## 通用瞬态与噪声残差

混合残差默认开启。正弦层继续使用固定频段预算；噪声层从正弦峰之外的高平坦度频谱残差中选择频带，并按平坦度置信度缩放；瞬态层以 25 ms 短窗和双阈值滞回检测快速起音。连续噪声频带使用可跟踪的确定性变体序列，相邻 100 ms 颗粒通过 50% 重叠窗交叉过渡，避免长期重复同一段噪声。

正弦层使用全局安全缩放；噪声和瞬态使用独立的逐帧限幅总线，默认上限分别为 0.70 和 0.40。瞬态优先使用峰值余量，可能削波时立即降低当前帧，之后约 200 ms 平滑恢复。逐层缩放统计写入分析报告的 `layer_scales.residual`。

兼容旧资源包时，资源包和转换命令必须同时关闭：

```bash
wav2mc bank-build --no-hybrid-residual --output output/legacy_bank.zip
wav2mc convert input.wav --no-hybrid-residual --bank-namespace wav2mc
```

## 4. 安装到 Minecraft

1. 将 `wav2mc_sine_bank.zip` 放入资源包目录并启用。
2. 将 `demo_song_datapack.zip` 放入目标世界的 `datapacks` 目录。
3. 进入世界后执行 `/reload`。
4. 以玩家身份运行：

```mcfunction
/function demo_song:start
```

停止：

```mcfunction
/function demo_song:stop
```

`start` 默认只把执行命令的玩家标记为监听者。再次从另一名玩家执行会切换监听者。

## 质量档位

| 质量 | 频率范围 | 正弦 | 噪声 | 瞬态 |
| --- | --- | ---: | ---: | ---: |
| `voice` | 80–8000 Hz | 12 | 2 | 2 |
| `normal` | 60–12000 Hz | 20 | 4 | 4 |
| `high` | 40–16000 Hz | 24 | 6 | 6 |
| `experimental` | 20–20000 Hz | 32 | 8 | 8 |

`normal` 的 20 个名额按频段硬限制为 4 / 6 / 7 / 3，分别对应 60–500、500–2000、2000–8000、8000–12000 Hz。某个频段没有足够候选峰时，其空余名额不会转给其他频段，因此高频不会挤占人声和旋律的主要分量。其他质量档也采用同样的固定分频预算策略。

示例：

```bash
wav2mc convert input.wav --quality high --gain 0.8
```

单独指定 `--quality` 只改变每帧选峰预算，不会自动切换资源包频率和 namespace。日常使用优先选择 `--mode high`，确保资源包与数据包完全匹配。

## 频率网格兼容

默认 `--frequency-grid adaptive` 使用上面的分段网格。只要显式提供 `--frequency-step`，就会切换到旧版均匀网格；也可以完整写明：

```bash
wav2mc bank-build --frequency-grid uniform --frequency-step 20
wav2mc convert input.wav --frequency-grid uniform --frequency-step 20
```

同一组自定义资源包和转换命令必须选择相同网格。设备模式固定使用其预设网格，不应再覆盖频率参数。

## 参数匹配

资源包和转换器的以下参数必须相同：

- `--sample-rate`
- `--grain-ms`
- `--hop-ms`
- `--min-frequency`
- `--max-frequency`
- `--frequency-grid`
- `--frequency-step`
- `--phases`
- `--hybrid-residual`
- `--residual-variants`
- `bank-build --grain-level` 与 `convert --bank-grain-level`
- 资源包 namespace 与转换时的 `--bank-namespace`
- 使用设备模式时，`bank-build-set` 与 `convert --mode` 的模式相同

转换报告 `*_analysis.json` 会记录所需资源包参数。

## 快速生成测试音频

```bash
python examples/make_test_tone.py
wav2mc convert test_tone.wav --name test_tone --max-frequency 2000
```

注意：如果资源包只生成到了 2000 Hz，转换命令也必须指定 `--max-frequency 2000`。

## 已知限制

- Minecraft 数据包只有 20 tick/s，客户端音频线程也不保证采样级同步。
- 窗口化可以减少阶跃咔嗒，但客户端卡顿仍可能造成音量凹陷或重叠误差。
- `/playsound` 无法直接设置连续相位，因此这里用离散相位音频文件近似。
- 游戏内立体声依赖两个头部相对的单声道声源和客户端空间混音，不是逐采样、无串音的双声道输出。
- 混合残差能补充瞬态和噪声纹理，但有限组件预算仍会产生声码器感。
- 同时播放大量声音可能受客户端声音通道、混音器或性能影响。
- 手动执行 `stop` 会立即停止 `record` 分类声音，可能切断正在播放的最后一个颗粒。

## 推荐开发方向

1. （已完成）给频率轨迹增加峰值跟踪，减少相邻帧频点抖动。
2. （已完成）改进幅值标定，使 Minecraft 实际响度更接近本地预览。
3. （已完成）增加心理声学掩蔽，而不是单纯按幅度选峰。
4. （已完成）生成多套分频资源包，按设备性能选择。
5. （已完成）增加通用瞬态与随机残差层，改善任意音频的快速起音和高频纹理。
6. 增加游戏内进度、暂停、跳转和多监听者控制。
7. 编写 Fabric 客户端模组，提供更准确的音频调度；数据包模式仍保留为兼容后端。

## 项目结构

```text
src/wav2mc/
├── analysis.py   正弦、噪声和瞬态分析
├── audio.py      FFmpeg 和 WAV I/O
├── bank.py       通用混合资源包
├── cli.py        命令行入口
├── config.py     默认参数和质量档位
├── datapack.py   数据包和帧分发树
├── gui.py        Tkinter 桌面界面
├── gui_state.py  GUI 设置与资源包状态
├── grains.py     确定性残差颗粒
├── loudness.py   Minecraft 响度校准模型
├── pipeline.py   转换流程
├── preview.py    本地重建预览
└── utils.py      通用工具
```
