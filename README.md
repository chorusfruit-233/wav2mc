# wav2mc 基础项目

把 WAV 或 FFmpeg 支持的其他音频文件转换成 Minecraft Java 数据包，并通过一个可复用的正弦颗粒资源包播放。

这个版本的目标是提供一个能运行、便于继续开发的基础架构，而不是高保真成品。它实现了：

- FFmpeg 解码、单声道化、重采样和带通滤波；
- 100 ms `sqrt-Hann` 正弦颗粒和 50 ms 帧移；
- STFT 幅度、相位分析；
- 按频段选取主要频率；
- 相邻帧峰值轨迹跟踪和频点切换迟滞；
- 频率和相位量化；
- 本地 `preview.wav` 重建；
- Minecraft 数据包 ZIP；
- 通用正弦资源包 ZIP；
- scoreboard 播放器和分层帧分发，避免长 `schedule` 链。

## 默认目标

默认参数按 Minecraft Java 1.21.7 设置：

- 资源包格式：64
- 数据包格式：81
- 现代目录：`data/<namespace>/function`
- 20 tick/s

其他版本请通过命令行覆盖 `--pack-format`、`--data-pack-format` 和 `--layout`。

## 环境

- Python 3.10+
- FFmpeg，且 `ffmpeg` 命令可在 PATH 中使用
- Python 包：NumPy、SoundFile

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

资源包参数必须和后续转换命令保持一致：

```bash
wav2mc bank-build \
  --output output/wav2mc_sine_bank.zip \
  --min-frequency 80 \
  --max-frequency 8000 \
  --frequency-step 20 \
  --phases 16 \
  --grain-level 1.0
```

输出资源包中包含：

```text
assets/wav2mc/sounds/grain/f0440/p00.ogg
assets/wav2mc/sounds/grain/f0440/p01.ogg
...
assets/wav2mc/sounds.json
```

每个颗粒为 100 ms，头尾由平方根 Hann 窗压到零附近。相邻 tick 以 50% 重叠播放，以降低片段边界的咔嗒声。

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

```text
low     每帧最多 8 个分量
normal  每帧最多 12 个分量
high    每帧最多 20 个分量
```

示例：

```bash
wav2mc convert input.wav --quality high --gain 0.8
```

## 参数匹配

资源包和转换器的以下参数必须相同：

- `--sample-rate`
- `--grain-ms`
- `--hop-ms`
- `--min-frequency`
- `--max-frequency`
- `--frequency-step`
- `--phases`
- `bank-build --grain-level` 与 `convert --bank-grain-level`
- 资源包 namespace 与转换时的 `--bank-namespace`

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
- 每帧只保留少量频率，人声、鼓点和噪声会有明显声码器感。
- 同时播放大量声音可能受客户端声音通道、混音器或性能影响。
- 手动执行 `stop` 会立即停止 `record` 分类声音，可能切断正在播放的最后一个颗粒。

## 推荐开发方向

1. （已完成）给频率轨迹增加峰值跟踪，减少相邻帧频点抖动。
2. 改进幅值标定，使 Minecraft 实际响度更接近本地预览。
3. 增加心理声学掩蔽，而不是单纯按幅度选峰。
4. 生成多套分频资源包，按设备性能选择。
5. 增加游戏内进度、暂停、跳转和多监听者控制。
6. 编写 Fabric 客户端模组，提供更准确的音频调度；数据包模式仍保留为兼容后端。

## 项目结构

```text
src/wav2mc/
├── analysis.py   STFT、选峰、相位量化
├── audio.py      FFmpeg 和 WAV I/O
├── bank.py       通用正弦资源包
├── cli.py        命令行入口
├── config.py     默认参数和质量档位
├── datapack.py   数据包和帧分发树
├── pipeline.py   转换流程
├── preview.py    本地重建预览
└── utils.py      通用工具
```
