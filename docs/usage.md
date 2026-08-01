# wav2mc 详细使用指南

wav2mc 把音频分析为每 tick 播放的正弦、带限噪声和瞬态颗粒，生成 Minecraft Java 数据包，并配合预先生成的资源包播放。完整工作流程是：

1. 为目标设备生成一次资源包。
2. 使用相同参数转换每个音频。
3. 先听本地预览 WAV，再安装 ZIP 到 Minecraft。

## 环境与安装

需要 Python 3.10+ 和 FFmpeg。桌面 GUI 还需要 Tk 8.6+。先确认 FFmpeg 可用：

```bash
ffmpeg -version
```

Linux/macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
wav2mc --help
```

Windows PowerShell：

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
wav2mc --help
```

每次打开新终端后都需要重新激活 `.venv`。不安装命令行入口时，也可在仓库根目录使用 `python run.py <command>`。

部分 Linux 发行版没有随 Python 默认安装 Tk。例如 Debian/Ubuntu 可运行 `sudo apt install python3-tk`，然后用以下命令检查：

```bash
python -c "import tkinter; print(tkinter.TkVersion)"
```

## 图形界面

直接启动：

```bash
wav2mc gui
```

启动时预选媒体文件和输出目录：

```bash
wav2mc gui /path/to/audio.m4s --output-dir output
```

重新执行 `python -m pip install -e .` 后，也可以运行独立入口 `wav2mc-gui`。

“音频转换”页提供输入文件、输出目录、歌曲名称、音轨索引、质量模式、dB 增益和心理声学掩蔽。增益范围为 -24～+12 dB，可直接输入或用箭头按 0.5 dB 调整；“归零”恢复到 0 dB（1.00x）。选择 `normal` 后生成的数据包使用 `wav2mc_normal` namespace，必须配合相同模式的资源包。

“资源包”页共享当前质量模式，可以只生成当前模式，或一次生成四个推荐模式。转换和资源包生成在后台线程执行；运行期间操作按钮会禁用，完成路径和 FFmpeg 错误会显示在窗口底部。

GUI 只暴露推荐的匹配预设。需要自定义频率网格、相位数、响度校准、资源包格式或旧版数据包布局时，使用下文的 CLI 命令。

## 输入格式

`wav2mc convert` 不依赖文件扩展名判断格式，而是交给 FFmpeg 探测和解码。常见输入包括：

- 无损/未压缩：WAV、FLAC、AIFF、ALAC。
- 有损：MP3、AAC、M4A、OGG/Vorbis、Opus、WMA。
- 媒体容器：含音轨的 MP4、MKV、WebM 等。

如容器包含多条音轨，默认选择第一条（索引 0）；可用 `--audio-stream 1` 选择第二条。视频、字幕和数据流会被忽略。输入必须是已存在的本地文件，当前不接受 URL 或标准输入。可用以下命令检查本机 FFmpeg 实际包含的解码器：

```bash
ffmpeg -decoders
```

文件无音轨、损坏或缺少对应解码器时，CLI 会返回 FFmpeg 的具体失败原因。

## 推荐快速流程

### 1. 生成设备档位资源包

```bash
wav2mc bank-build-set --output-dir output/device_banks
```

会生成：

```text
output/device_banks/wav2mc_voice_sine_bank.zip
output/device_banks/wav2mc_normal_sine_bank.zip
output/device_banks/wav2mc_high_sine_bank.zip
output/device_banks/wav2mc_experimental_sine_bank.zip
output/device_banks/wav2mc-device-packs.json
```

| 模式 | 频率范围 | 相位 | 正弦 | 噪声 | 瞬态 |
| --- | --- | ---: | ---: | ---: | ---: |
| `voice` | 80–8000 Hz | 8 | 12 | 2 | 2 |
| `normal` | 60–12000 Hz | 12 | 20 | 4 | 4 |
| `high` | 40–16000 Hz | 16 | 24 | 6 | 6 |
| `experimental` | 20–20000 Hz | 16 | 32 | 8 | 8 |

可以只生成需要的档位：

```bash
wav2mc bank-build-set --profiles voice normal
```

为兼容旧命令仍保留 `low`，但默认资源包集不再生成它。

当前 ZIP 文件名为兼容已有脚本仍包含 `_sine_bank`，但包内已经同时包含正弦、噪声和瞬态事件。旧资源包必须重新生成后才能播放新转换结果。

### 2. 用同一档位转换音频

```bash
wav2mc convert input.wav \
  --name demo_song \
  --mode normal \
  --output-dir output
```

`--mode normal`（等价于 `--device-profile normal`）会同时选择 60–12000 Hz 自适应网格、12 相位、`normal` 质量和 `wav2mc_normal` namespace。不要混用不同模式的资源包与转换结果。

转换器会通过 FFmpeg 解码第一条音轨，然后单声道化、重采样并带通滤波。

### 3. 检查输出

```text
output/demo_song_datapack.zip
output/demo_song_preview.wav
output/demo_song_analysis.json
```

- `*_preview.wav`：用与数据包相同的频点、相位和幅值重建，应先试听。
- `*_datapack.zip`：安装到世界的数据包。
- `*_analysis.json`：记录质量、帧数、命令负载、校准值和所需资源包参数。

## 安装到 Minecraft 26.2

1. 把选定档位的 `wav2mc_*_sine_bank.zip` 放入 Minecraft `resourcepacks` 目录并启用。
2. 把 `demo_song_datapack.zip` 放入 `<world>/datapacks/`。
3. 进入世界后执行 `/reload`。
4. 由需要听到声音的玩家执行：

```mcfunction
/function demo_song:start
```

停止播放：

```mcfunction
/function demo_song:stop
```

`demo_song` 来自 `--name`，会被转换为合法 namespace。`start` 会把执行者设为当前监听者；其他玩家再次执行会切换监听者。请确认游戏中 `Records/Jukebox` 声音分类未静音。

## 自定义资源包

不使用设备模式时，可完全控制频率银行。默认自适应网格为：

| 频率范围 | 间隔 |
| --- | ---: |
| 20–1000 Hz | 20 Hz |
| 1000–4000 Hz | 40 Hz |
| 4000–8000 Hz | 80 Hz |
| 8000–12000 Hz | 160 Hz |
| 12000–20000 Hz | 320 Hz |

完整网格包含 225 个频点，比在全频段保持 20 Hz 间隔小得多。频段边界只生成一次；如果自定义上限不落在网格上，实际最高频点是上限以下最后一个网格点。例如 `high` 截止于 16000 Hz，最后一个频点为 15840 Hz。

生成自适应网格资源包：

```bash
wav2mc bank-build \
  --output output/custom_bank.zip \
  --namespace custom_bank \
  --min-frequency 80 \
  --max-frequency 12000 \
  --frequency-grid adaptive \
  --phases 16
```

如需复现旧版均匀 20 Hz 网格，显式提供 `--frequency-step` 即可切换，也可以同时指定 `--frequency-grid uniform`：

```bash
wav2mc bank-build \
  --output output/custom_bank.zip \
  --namespace custom_bank \
  --min-frequency 100 \
  --max-frequency 6000 \
  --frequency-grid uniform \
  --frequency-step 20 \
  --phases 16 \
  --grain-level 1.0
```

转换时必须使用相同参数：

```bash
wav2mc convert input.wav \
  --name custom_song \
  --bank-namespace custom_bank \
  --min-frequency 100 \
  --max-frequency 6000 \
  --frequency-grid uniform \
  --frequency-step 20 \
  --phases 16 \
  --bank-grain-level 1.0
```

必须匹配的参数包括 `sample-rate`、`grain-ms`、`hop-ms`、频率范围、网格类型、频率步长（均匀网格）、相位数、混合残差开关、残差变体数、颗粒音量和 namespace。不匹配时通常会出现缺失声音事件、音量错误或频率缺口。

## 转换参数

### 质量和性能

- `--mode voice|normal|high|experimental`：同时匹配资源包频率、相位、质量和 namespace，日常使用优先选择。
- `--quality voice|normal|high|experimental`：改变正弦、噪声和瞬态预算；不会自动切换资源包。
- `--gain`：转换增益，必须大于 0。输出峰值仍会受安全缩放限制。
- `--category`：Minecraft 声音分类，默认 `record`。

`normal` 每帧的 20 个分量不是全频段统一竞争，而是采用硬预算：

| 频段 | 每帧最多保留 |
| --- | ---: |
| 60–500 Hz | 4 |
| 500–2000 Hz | 6 |
| 2000–8000 Hz | 7 |
| 8000–12000 Hz | 3 |

各频段只在自己的预算内选取峰值。空余名额不会跨频段转移，因此高频主要补充明亮度，不会抢走人声和旋律分量。`voice`、`high`、`experimental` 也使用按各自总预算扩展的固定分频限制；精确数值会写入 `*_analysis.json` 的 `quality_profile.band_budgets`。

### 通用混合残差

混合残差默认开启，并且不依赖乐器分类：

- 正弦层使用现有 100 ms 分析窗，保留旋律、人声元音和持续音。
- 噪声层通过频谱平坦度选择 16 个感知频带中的随机残差，保留齿音、环境声、失真和镲片纹理。
- 瞬态层使用 25 ms 窗、10 ms 帧移和频谱通量，保留所有快速起音、点击、辅音和打击声。

正弦与残差采用独立峰值标定，瞬态峰值不会触发整首音乐同比降音量。`normal/high/experimental` 每帧额外最多使用 4/6/8 个噪声和 4/6/8 个瞬态组件；瞬态只在检测到起音时出现。

- `--no-hybrid-residual`：关闭通用残差，用于配合升级前的纯正弦资源包。
- `--residual-variants`：每个频带的确定性噪声变体数，默认 4；资源包和转换命令必须相同。

### 频率与时间分辨率

- `--sample-rate`：采样率，默认 48000 Hz。
- `--grain-ms`：颗粒/分析窗长，默认 100 ms。
- `--hop-ms`：帧移，默认 50 ms。当前必须满足 `grain-ms = 2 * hop-ms`。
- `--min-frequency` / `--max-frequency`：分析与资源包的频率范围。
- `--frequency-grid adaptive|uniform`：默认 `adaptive`，按频段逐步增大间隔。
- `--frequency-step`：均匀网格的频点间距。显式提供该参数会选择 `uniform`；更小会增加频率精度、资源包体积和生成时间。
- `--phases`：离散相位数。更大会降低相位量化误差，但按比例扩大资源包。

### 心理声学掩蔽

掩蔽默认开启。A-weighting 用于感知显著度排序，Bark 频带模型剔除强峰附近难以听见的弱峰。

- `--masking-offset-db`：越高越保守，会保留更多峰值。`voice/normal/high/experimental` 默认为 9/10/14/16 dB。
- `--no-psychoacoustic-masking`：关闭 Bark 掩蔽，适合做 A/B 比较或诊断丢失的频率。

## Minecraft 响度校准

默认模型为：

```text
预测游戏幅值 = bank_grain_level
                 * minecraft_gain
                 * command_volume ^ volume_exponent
```

默认 `minecraft_gain=1`、`volume_exponent=1`，即线性响应。如在相同系统音量下，游戏内捕获到的幅值约为预览的 85%，可使用：

```bash
wav2mc convert input.wav \
  --minecraft-gain 0.85 \
  --minecraft-volume-exponent 1.0 \
  --max-command-volume 1.0
```

- `--minecraft-gain`：命令音量为 1.0 时的“实测游戏幅值 / 参考幅值”。
- `--minecraft-volume-exponent`：用于拟合中等命令音量的非线性。未测量时保持 1.0。
- `--max-command-volume`：数据包可写入的最大 `/playsound` 音量。
- `--bank-grain-level`：必须等于生成资源包时的 `--grain-level`。

转换器会反解命令音量。如需要的分量幅值超过模型可复现范围，会同比降低数据包和本地预览，而不是只截断 Minecraft 音量。该模型需要根据具体客户端、声音分类音量和录音链路实测；默认值不代表所有设备。

## 版本与布局兼容

默认目标为 Minecraft Java 26.2：

- Resource Pack 格式 `88.0`
- Data Pack 格式 `107.1`
- 数据包函数目录 `data/<namespace>/function`

目标其他版本时，资源包使用 `--pack-format`，转换使用 `--data-pack-format`。旧版本如果要求 `functions` 复数目录，再为转换命令添加 `--layout legacy`。格式号必须查询目标 Minecraft 版本的官方发布说明，不要沿用 26.2 的默认值。

## 读取分析报告

`*_analysis.json` 中建议重点检查：

- `minecraft_version` 和 `data_pack.pack_format`：目标版本。
- `required_resource_pack`：实际需要的 namespace、频率银行、相位数和颗粒音量。
- `audio_config.frequency_grid`、`frequency_bands` 和 `frequency_count`：实际网格与资源数量。
- `quality_profile.band_budgets`：每个频段的硬分量预算。
- `component_model`：三层的平均与最大组件数量。
- `layer_scales`：正弦层和残差层各自的安全缩放。
- `preview_peak`：本地预览的峰值。
- `average_components_per_frame` 和 `estimated_playsound_commands_per_second`：客户端负载指标。
- `loudness_calibration.maximum_predicted_amplitude_error`：命令音量保留 6 位小数后的模型误差。

## 常见问题

### `ffmpeg` 未找到

安装 FFmpeg 并确保 `ffmpeg -version` 可在同一终端运行。仅安装 Python 包不会自动安装 FFmpeg。

### 进入游戏后完全无声

先用与所装资源包对应的 namespace 直接测试一个声音事件。例如 `normal` 包应能播放：

```mcfunction
/playsound wav2mc_normal:grain.f0440.p00 record @s ~ ~ ~ 1 1
```

如果这条命令无声，检查资源包已启用、`Records/Jukebox` 音量未静音，并查看游戏日志中的资源加载或缺失声音错误。如果能听到测试音，则资源包正常；再执行 `/reload` 和 `/function <name>:start`，并确认 `<name>` 与数据包文件内的 namespace 一致。分析报告的 `required_resource_pack.namespace` 必须与 `/playsound` 前缀一致。

### 预览就有明显失真

先确认已重新生成混合资源包，再尝试 `--mode high`，或在资源允许时使用 `--mode experimental`。如某些弱频率被掩蔽，提高 `--masking-offset-db` 或临时关闭掩蔽做对照。分析报告中 `component_model` 的噪声和瞬态数量持续为零时，确认没有使用 `--no-hybrid-residual`。

### 本地预览正常，游戏内音量不对

先检查 Minecraft 主音量和声音分类。如仍有稳定差异，使用 `--minecraft-gain` 校准；只有中间音量偏差时再调整 `--minecraft-volume-exponent`。

### 客户端卡顿或声音断续

改用 `normal` 或 `voice` 模式，或自建频率范围更窄、相位更少的资源包。在分析报告中比较每帧分量和预计每秒命令数。均匀网格用户也可以增大 `--frequency-step`。

## 完整示例

```bash
# 一次性生成 normal 资源包
wav2mc bank-build-set \
  --profiles normal \
  --output-dir output/device_banks

# 每首歌单独转换
wav2mc convert music/example.flac \
  --name example_song \
  --mode normal \
  --gain 0.9 \
  --output-dir output
```

安装 `output/device_banks/wav2mc_normal_sine_bank.zip` 和 `output/example_song_datapack.zip`，然后在游戏中执行 `/reload` 和 `/function example_song:start`。
