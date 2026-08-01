# wav2mc 详细使用指南

wav2mc 把音频分析为每 tick 播放的正弦颗粒，生成 Minecraft Java 数据包，并配合预先生成的资源包播放。完整工作流程是：

1. 为目标设备生成一次资源包。
2. 使用相同参数转换每个音频。
3. 先听本地预览 WAV，再安装 ZIP 到 Minecraft。

## 环境与安装

需要 Python 3.10+ 和 FFmpeg。先确认 FFmpeg 可用：

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
output/device_banks/wav2mc_low_sine_bank.zip
output/device_banks/wav2mc_normal_sine_bank.zip
output/device_banks/wav2mc_high_sine_bank.zip
output/device_banks/wav2mc-device-packs.json
```

| 档位 | 频率范围 | 步长 | 相位数 | 每帧最多分量 | 用途 |
| --- | --- | ---: | ---: | ---: | --- |
| `low` | 80–2000 Hz | 40 Hz | 8 | 8 | 性能较低的客户端 |
| `normal` | 80–4000 Hz | 20 Hz | 8 | 12 | 推荐默认选择 |
| `high` | 80–8000 Hz | 20 Hz | 16 | 20 | 更高音质和命令负载 |

可以只生成需要的档位：

```bash
wav2mc bank-build-set --profiles low normal
```

### 2. 用同一档位转换音频

```bash
wav2mc convert input.wav \
  --name demo_song \
  --device-profile normal \
  --output-dir output
```

`--device-profile normal` 会同时选择 4000 Hz 上限、20 Hz 步长、8 相位、`normal` 质量和 `wav2mc_normal` namespace。不要用 `low` 资源包播放 `normal` 或 `high` 转换结果。

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

不使用设备档位时，可完全控制频率银行：

```bash
wav2mc bank-build \
  --output output/custom_bank.zip \
  --namespace custom_bank \
  --min-frequency 100 \
  --max-frequency 6000 \
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
  --frequency-step 20 \
  --phases 16 \
  --bank-grain-level 1.0
```

必须匹配的参数包括 `sample-rate`、`grain-ms`、`hop-ms`、频率范围、频率步长、相位数、颗粒音量和 namespace。不匹配时通常会出现缺失声音事件、音量错误或频率缺口。

## 转换参数

### 质量和性能

- `--quality low|normal|high`：分别限制每帧最多 8、12、20 个分量。档位越高，保留的细节和每秒 `/playsound` 命令越多。
- `--gain`：转换增益，必须大于 0。输出峰值仍会受安全缩放限制。
- `--category`：Minecraft 声音分类，默认 `record`。

### 频率与时间分辨率

- `--sample-rate`：采样率，默认 48000 Hz。
- `--grain-ms`：颗粒/分析窗长，默认 100 ms。
- `--hop-ms`：帧移，默认 50 ms。当前必须满足 `grain-ms = 2 * hop-ms`。
- `--min-frequency` / `--max-frequency`：分析与资源包的频率范围。
- `--frequency-step`：频点间距。更小会增加频率精度、资源包体积和生成时间。
- `--phases`：离散相位数。更大会降低相位量化误差，但按比例扩大资源包。

### 心理声学掩蔽

掩蔽默认开启。A-weighting 用于感知显著度排序，Bark 频带模型剔除强峰附近难以听见的弱峰。

- `--masking-offset-db`：越高越保守，会保留更多峰值。`low/normal/high` 默认为 7/10/14 dB。
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
- `preview_peak`：本地预览的峰值。
- `average_components_per_frame` 和 `estimated_playsound_commands_per_second`：客户端负载指标。
- `loudness_calibration.maximum_predicted_amplitude_error`：命令音量保留 6 位小数后的模型误差。

## 常见问题

### `ffmpeg` 未找到

安装 FFmpeg 并确保 `ffmpeg -version` 可在同一终端运行。仅安装 Python 包不会自动安装 FFmpeg。

### 进入游戏后完全无声

确认资源包已启用，档位/namespace 与分析报告一致，执行过 `/reload` 和 `/function <name>:start`，且 `Records/Jukebox` 音量未静音。查看游戏日志中是否有缺失声音事件。

### 预览就有明显失真

先尝试 `--quality high`、提高设备档位或频率上限。如某些弱频率被掩蔽，提高 `--masking-offset-db` 或临时关闭掩蔽做对照。对人声、鼓点和噪声，稀疏正弦重建仍会有明显声码器感。

### 本地预览正常，游戏内音量不对

先检查 Minecraft 主音量和声音分类。如仍有稳定差异，使用 `--minecraft-gain` 校准；只有中间音量偏差时再调整 `--minecraft-volume-exponent`。

### 客户端卡顿或声音断续

改用 `normal` 或 `low` 设备档位，降低 `--quality`，缩小频率上限，或增大 `--frequency-step`。在分析报告中比较每帧分量和预计每秒命令数。

## 完整示例

```bash
# 一次性生成 normal 资源包
wav2mc bank-build-set \
  --profiles normal \
  --output-dir output/device_banks

# 每首歌单独转换
wav2mc convert music/example.flac \
  --name example_song \
  --device-profile normal \
  --gain 0.9 \
  --output-dir output
```

安装 `output/device_banks/wav2mc_normal_sine_bank.zip` 和 `output/example_song_datapack.zip`，然后在游戏中执行 `/reload` 和 `/function example_song:start`。
