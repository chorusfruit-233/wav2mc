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

### 转换工作台

选择文件后，GUI 会在后台调用 `ffprobe`，显示时长、编码、采样率和声道，并把容器中的音频流填入“高级设置”的音轨列表。探测失败不会阻止转换，此时回退到音轨 0；FFmpeg 在实际解码时仍会给出具体错误。

左栏设置输入、歌曲名称和输出目录。右栏选择 `voice`、`normal`、`high` 或 `experimental`，并用 -24～+12 dB 滑块或 0.5 dB 数值框调整增益；圆形箭头恢复 0 dB（1.00x）。“高级设置”包含音轨、立体声保留和心理声学掩蔽。摘要中的最大命令负载按当前档位和声道数计算。选择 `normal` 后，数据包使用 `wav2mc_normal` namespace，必须配合相同档位的资源包。

### 进度、取消与结果

解码、分析、预览重建、数据包和报告阶段使用确定进度条。运行时主按钮变为“取消”；取消可能需要等待当前 FFmpeg 或压缩操作退出。WAV、数据包和报告先写入同目录暂存区，完整成功后才替换正式文件，因此取消或失败不会留下半成品，也不会覆盖已有同名成品。

成功后不会弹出阻塞对话框。固定结果栏显示音频时长、输出声道、预览峰值、实际命令数和所需资源包状态，并提供以下操作：

- “试听”提供本地预览的播放/暂停、停止、时间显示和进度拖动。需要 `ffplay` 在 PATH 中可用；缺少时会提示并改用系统默认播放器，此时在外部播放器中控制播放。试听时间显示为本地时钟估计值，暂停后继续或拖动位置会重新启动音频输出，可能有短暂延迟。关闭结果栏、替换转换结果或退出窗口会停止受控试听。
- “打开输出目录”定位三个输出文件。
- “命令”菜单可复制开始、暂停、继续、停止、前后跳转、循环、进度和多人收听命令；这些命令需粘贴到 Minecraft 执行。旧报告只提供启动命令。
- 资源包缺失或参数不匹配时，“生成匹配资源包”会切换到资源包页并预选所需档位。

底部“任务详情”默认折叠；失败时自动展开。关闭正在运行的窗口时，确认后会先发出取消请求，等待工作线程退出再关闭。

### 资源包与设置记忆

“资源包”页列出四个推荐档位，可勾选任意组合后一次生成。每行状态含义如下：

- “有效”：ZIP 中的 namespace、档位、频率网格、相位、颗粒窗口、残差参数和 Minecraft 版本均匹配。
- “缺失”：目标目录中没有该档位 ZIP。
- “参数不匹配”：ZIP 损坏、过旧或由不同参数生成，应重新生成。

GUI 自动保存最近输入目录、转换输出目录、资源包目录、模式、增益、立体声、掩蔽、高级区状态和资源包勾选项。Linux 配置位于 `$XDG_CONFIG_HOME/wav2mc/gui.json`，未设置时使用 `~/.config/wav2mc/gui.json`；Windows 使用 `%APPDATA%\wav2mc\gui.json`，macOS 使用 `~/Library/Application Support/wav2mc/gui.json`。配置损坏时会静默恢复默认值。启动命令显式传入的文件和 `--output-dir` 优先于记忆值。

GUI 只暴露推荐的匹配预设。需要自定义频率网格、相位数、响度校准、资源包格式或旧版数据包布局时，使用下文的 CLI 命令。

## 输入格式

`wav2mc convert` 不依赖文件扩展名判断格式，而是交给 FFmpeg 探测和解码。常见输入包括：

- 无损/未压缩：WAV、FLAC、AIFF、ALAC。
- 有损：MP3、AAC、M4A、OGG/Vorbis、Opus、WMA。
- 媒体容器：含音轨的 MP4、MKV、WebM 等。

如容器包含多条音轨，默认选择第一条（索引 0）；可用 `--audio-stream 1` 选择第二条。默认只转换音频，添加 `--video` 可同步转换视频画面；字幕和数据流忽略。输入必须是已存在的本地文件，当前不接受 URL 或标准输入。可用以下命令检查本机 FFmpeg 实际包含的解码器：

```bash
ffmpeg -decoders
```

文件无音轨、损坏或缺少对应解码器时，CLI 会返回 FFmpeg 的具体失败原因。

### 立体声输入

默认 `--stereo` 会保留最多两个声道。左右声道分别执行正弦、噪声和瞬态分析，`*_preview.wav` 写成真正的双声道文件；数据包使用玩家本地坐标，在左侧 `^0.75 ^ ^` 和右侧 `^-0.75 ^ ^` 播放对应的单声道资源颗粒。玩家转身时声场会跟随朝向。

双单声道输入会自动折叠为单声道。需要降低分析时间和游戏内命令负载时使用：

```bash
wav2mc convert input.wav --mode normal --no-stereo
```

立体声只改变分析结果和数据包，不增加资源包事件，因此无需为它单独生成资源包。Minecraft 客户端使用空间音频混合两个声源，实际串音和声像宽度会受“定向音频”、耳机/扬声器设置及声音引擎影响。

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

转换器会通过 FFmpeg 解码第一条音轨，然后保留立体声（或按要求下混）、重采样并带通滤波。

### 3. 检查输出

```text
output/demo_song_datapack.zip
output/demo_song_preview.wav
output/demo_song_analysis.json
```

- `*_preview.wav`：用与数据包相同的频点、相位和幅值重建，应先试听。
- `*_datapack.zip`：安装到世界的数据包。
- `*_analysis.json`：记录质量、帧数、命令负载、校准值和所需资源包参数。

## 安装到 Minecraft 26.3

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

`demo_song` 来自 `--name`，会被转换为合法 namespace。`start` 从头播放并把执行者设为本曲监听者；其他玩家用 `join` 同步加入。请确认游戏中 `Records/Jukebox` 声音分类未静音。

歌曲名称支持中文。例如 `--name "中文歌曲"` 会输出 `中文歌曲_datapack.zip`、`中文歌曲_preview.wav` 和 `中文歌曲_analysis.json`；未指定 `--name` 时使用输入文件名（不含扩展名）。文件名保留大小写和空格，并替换路径分隔符及非法文件名字符。中文名称在包内会使用带稳定摘要后缀的合法 namespace，防止不同歌曲发生冲突。游戏内播放命令请使用 GUI 的“命令”菜单，或读取分析报告中的 `song_namespace`，不要直接将中文文件名填入 `/function`。

### 播放控制

每个歌曲 namespace 对应独立的时间轴、循环设置和监听者，不同歌曲的数据包可以同时安装。相同 namespace 的数据包仍会互相覆盖，需要用不同歌曲名称重新转换。旧数据包需重新转换以获得下列控制；匹配的资源包无需重建。

| 命令后缀 | 行为 |
| --- | --- |
| `start` | 从头播放，将执行玩家设为本曲唯一监听者；不能直接从控制台调用 |
| `start_all` | 从头播放给所有当前在线玩家，可从控制台调用 |
| `join` / `leave` | 执行玩家加入 / 离开本曲收听；加入不改变进度 |
| `pause` / `resume` | 暂停并保留位置 / 从当前位置继续 |
| `stop` | 停止、归零并清除本曲监听者 |
| `seek_forward` / `seek_backward` | 前进 / 后退 10 秒，超出范围时限制在歌曲边界 |
| `loop_on` / `loop_off` | 开启 / 关闭本曲循环 |
| `status` | 在聊天栏查询播放状态、位置、总时长和循环设置 |
| `help` | 显示本曲控制命令与任意位置跳转用法 |

例如：

```mcfunction
/function demo_song:pause
/function demo_song:resume
/function demo_song:seek_forward
/function demo_song:loop_on
/function demo_song:status
```

多人收听时，由一名玩家运行 `start`，其他玩家运行 `/function demo_song:join`。重新执行 `start` 会重新开始并替换本曲监听者。无在线监听者时自动暂停；加入后使用 `resume` 继续。停止、重新开始或 `/reload` 会使旧会话失效，离线玩家重新连接后需重新 `join`。自然结束保留最后一个颗粒的尾音。

跳转到指定的整数秒位置，先给本曲 `#seek` 计分项赋值，再运行 `seek`。`demo_song` 的计分板名称示例如下，其他歌曲请读取报告的 `playback.objective` 或运行 `help` 获取实际名称：

```mcfunction
/scoreboard players set #seek w2m_a23f7f35a522 30
/function demo_song:seek
```

负数跳转到开头，超过总时长则到结尾；暂停时跳转到曲内位置保持暂停，跳到结尾会结束播放。自然结束或跳到结尾后，可用 `resume` 从头重播给现有监听者。计分项使用秒，内部时间轴为 20 tick/s；`playback.duration_seconds` 是帧序列长度，可能与输入时长或包含尾音的预览时长略有差异。

控制台 / 命令方块若要为指定玩家开始播放，使用 `execute as <玩家> run function demo_song:start`；不要用 `execute as @a ...:start`，因为每次调用都会重新开始。所有在线玩家从头收听应使用 `start_all`。玩家调用的控制作用于整首歌的共享会话，仍需相应命令权限。

暂停、跳转、离开和停止只停止本曲实际使用的声音事件，不清空整个 `record` 分类。Minecraft 无法区分同一事件的不同播放实例；同一玩家同时收听其他使用相同音粒的歌曲时，那些重叠音粒也可能被切断。

## 展示实体视频

勾选 GUI 左栏的“视频（展示实体）”，或在转换命令中添加 `--video`：

```bash
wav2mc convert input.mp4 --video --mode normal --name demo_video
```

固定分辨率档通过 FFmpeg 保留显示比例、补黑边后输出 RGB 画面。原分辨率档保留自动旋转后的源像素网格，不进行空间缩放，并保留非方形像素的显示比例。程序自动选择逐像素背景或分块纹理展示实体；纹理模式会同时生成必须启用的视频资源包。使用音轨时仍需安装匹配的混合音频资源包；无音轨视频会自动生成无声播放，也可用 `--no-audio` 忽略已有音轨。

### 视频参数

画质预设会同时设置分辨率、帧率和像素边长。固定档屏幕为 3.2×1.8 格，原分辨率档宽 3.2 格、高度保持源显示比例：

| 画质 | `--video-quality` | 分辨率 | FPS | 自动渲染 | 画面实体数 | 实体更新/秒 |
| --- | --- | --- | ---: | --- | ---: | ---: |
| 低 | `low` | 64×36 | 10 | 逐像素背景 | 2304 | 23040 |
| 普通（默认） | `normal` | 160×90 | 10 | 分块纹理 | 1 | 10 |
| 高 | `high` | 320×180 | 20 | 分块纹理 | 2 | 40 |
| 超高 | `ultra` | 640×360 | 20 | 分块纹理 | 6 | 120 |
| 原分辨率 | `native` | 源视频尺寸 | 20 | 分块纹理 | 按 256×256 分块 | 实体数 × FPS |

每块屏幕另有 1 个定位实体。低档无需视频资源包，因而仍使用逐像素背景；纹理档以资源包纹理缓存换取较少实体和更新命令。表中不含音频与分发命令。`--video-quality` 与音频的 `--mode`、`--quality` 独立，例如原分辨率配普通音频：

```bash
wav2mc convert input.mp4 --video --video-quality native --mode normal
```

GUI 的“画质”可选择低、普通、高、超高、原分辨率或自定义。原分辨率会读取源视频尺寸，分辨率输入框随源视频显示且不缩放；帧率仍可单独选择。界面显示实际展示实体数、更新量、屏幕尺寸和纹理缓存估算。已有自定义设置保留原参数。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--video-quality` | `normal` | 视频画质预设，需同时启用 `--video` |
| `--video-width` / `--video-height` | 跟随画质 | 正整数；纹理模式不再有 128 边长和 4096 像素限制 |
| `--video-fps` | 跟随画质 | 覆盖帧率，可选 1、2、4、5、10、20，均可与 20 tick/s 时间轴对齐 |
| `--video-pixel-size` | 跟随画质 | 覆盖单个像素的边长，单位格，需大于 0 且不超过 2 |
| `--video-stream` | 0 | 零起始视频流索引，封面图不作为视频使用 |
| `--video-renderer` | `auto` | `auto` 对原分辨率或超过 4096 像素的画面使用 `texture`，否则使用 `pixels`；可显式覆盖 |
| `--video-resource-pack-format` | 97.1 | 生成的视频资源包格式，目标旧版本时与数据包版本分别设置 |
| `--no-audio` | 关闭 | 只播放视频，不需要音频资源包 |

例如 1920×1080 原分辨率分为 8×5 块，只用 40 个画面实体和 1 个定位实体，20 FPS 时每秒约 800 次画面更新。纹理模式保留每个 RGB 像素，右下补齐区域透明，不增加可见画面尺寸。原分辨率不等于原帧率：Minecraft 时间轴仍为 20 tick/s，源视频超过 20 FPS 时会按所选帧率采样。需要减少纹理缓存时可降低帧率：

```bash
wav2mc convert input.mp4 --video --no-audio \
  --video-quality native --video-fps 5
```

显式参数优先于预设，例如 `--video-quality high --video-fps 5` 保留 320×180 分辨率并改为 5 FPS。原分辨率只修改帧率或像素边长时仍保留源像素网格；若同时显式指定宽和高则转为固定尺寸自定义模式，只指定其中一项会报错。强制 `pixels` 模式仍限制最多 4096 个像素实体。切换画质需重新转换数据包与视频资源包，匹配的音频资源包可继续使用。

视频解码使用临时磁盘帧文件；PNG 逐行压缩，不把所有帧载入 Python 内存。取消或失败会清理暂存文件并保留已有输出。纹理模式比原有数据包、音频预览 WAV 和分析报告多输出一个 `*_video_resourcepack.zip`。无声视频的 WAV 为静音。桌面“试听”仍只播放转换后的音轨。

安装纹理视频时，将 `*_datapack.zip` 放到世界的 `datapacks`，将 `*_video_resourcepack.zip` 放入每位观看玩家客户端的 `resourcepacks` 并启用；含音轨时另外启用匹配的音频资源包。所有包内 namespace 与转换结果对应，不要混用不同视频的视频资源包。

原分辨率视频仍需要较多客户端内存：已播放的字形纹理会被缓存，每帧每块约占 256×256×4 字节。1920×1080 的每帧约需 10 MiB 纹理缓存，20 FPS 的 2 秒视频估算约 400 MiB，此外还有源图与客户端开销。报告的 `estimated_texture_bytes` 是估算值，不能视为客户端流畅度保证。长视频宜先用短片段验证，并按需要降低帧率或分辨率。

### 放置与控制屏幕

安装数据包后由玩家执行：

```mcfunction
/function demo_video:start
```

首次播放自动在执行玩家前方约 3 格、中心高于脚底 1.6 格的位置放置屏幕，朝向执行玩家的水平方向。屏幕固定在世界中，多名监听者观看同一块屏幕。再次播放会沿用已加载的屏幕位置；需要移动时，在新位置执行：

```mcfunction
/function demo_video:video/screen_create
```

这会移除本视频旧屏幕并按当前进度重新绘制，不改变播放位置。仅隐藏画面、继续播放音频时使用：

```mcfunction
/function demo_video:video/screen_remove
```

开始、暂停、继续、跳转、循环和多人收听使用[同一组播放命令](#播放控制)。暂停保持当前画面，跳转立即刷新对应画面，循环从第一帧重播。自然结束保留最后一帧，`stop` 或 `/reload` 移除本视频屏幕。图像和音频共用 20 tick/s 时间轴；较短的视频停留在最后一帧，较短的音轨结束后继续播放剩余视频。自定义音频参数时，视频模式要求 50 ms 帧移。

每个视频 namespace 管理一块屏幕，多个不同 namespace 的视频可以共存。支持主世界、下界和末地，不支持自定义维度。屏幕实体位于同一区块，未加载时不显示；程序不会自动强制加载区块。旧屏幕在卸载期间无法立即删除，重新加载后会根据会话编号自动清理。

视频需要 Minecraft Java 1.19.4+（数据包格式至少 12）。1.21.5 起文本组件使用 SNBT；程序按 `--data-pack-format` 自动选择编码，旧版本应同时选择对应格式与目录布局，不能仅修改 `--layout`。默认格式仍针对 26.3。使用改写默认字体的资源包可能改变像素背景尺寸。

分析报告、数据包和视频资源包的 `video` 元数据一致，包含 `quality` / `quality_label`、原分辨率标记、源尺寸、实际尺寸、`display_entities`、`display_updates_per_second`、`estimated_texture_bytes`、资源包需求和屏幕命令。`pixel_updates_per_second` 表示逻辑像素量，纹理模式的实际命令更新量应读取 `display_updates_per_second`；`playback.duration_seconds` 是完整音视频时间轴长度。

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

当前正弦颗粒使用 `Hann` 窗，在 50% 重叠时减少周期性音量起伏；分析会校正窗口的幅度衰减。噪声颗粒仍使用 `sqrt-Hann` 窗，并按各自频带的平坦度确定幅度。这些改进不会增加每帧声音命令预算。

本轮音粒库升级到 `bank_revision: 2`，需要重新生成资源包并重新转换音频，包括已经使用 `Hann` 窗的旧包。GUI 会将缺少版本标记或参数不符的旧包判为“参数不匹配”。CLI 用户也应重建对应包。Minecraft 包格式号保持不变。

新版音粒库包含：

- 多个相近频谱峰可共用基础频率，各自设置音高，避免高频复音丢失。
- 线性滑音音粒用于颤音和快速音高变化，普通正弦音粒仍用于稳定音高。
- 首尾专用包络补偿持续音的边界衰减；自然淡入、淡出会按边缘能量判定，避免强行抬高。
- 瞬态音粒支持 0–45 ms、每 5 ms 一档的延迟，以及快速、中等和缓慢三种衰减，延迟后的尾音完整保留。
- 噪声音粒按频带的左右声道相关性选择变体和极性，保留单声道、反相和独立环境声的空间关系。
- OGG 编码使用显式高质量设置（SoundFile 0.13+），预览与资源包共用编码流程。

这些变体增加资源包体积和生成时间，但不会增加每帧的声音命令上限。实际使用的命令数仍取决于输入内容和质量档位。

GUI 在所选目录中找到匹配资源包时，会直接读取其 OGG 音粒生成预览。CLI 使用 `--preview-resource-pack 路径.zip`；该包的频率、相位、音粒库版本、颗粒音量和 namespace 必须与转换参数一致，不匹配会报错。未指定时，转换器会生成同规格 OGG 音粒再解码，预览使用线性插值模拟音高变速；它不模拟客户端的声音调度、空间化或服务器卡顿。

## 转换参数

### 质量和性能

- `--mode voice|normal|high|experimental`：同时匹配资源包频率、相位、质量和 namespace，日常使用优先选择。
- `--quality voice|normal|high|experimental`：改变正弦、噪声和瞬态预算；不会自动切换资源包。
- `--gain`：转换增益，必须大于 0。输出峰值仍会受安全缩放限制。
- `--stereo` / `--no-stereo`：默认保留立体声；关闭后在分析前下混为单声道。
- `--category`：Minecraft 声音分类，默认 `record`。

立体声会让每帧预算和 `/playsound` 命令数接近翻倍。游戏内优先使用 `normal` 或 `high`；`experimental` 立体声可能超过部分客户端能稳定处理的声音通道和命令负载。

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
- 瞬态层使用 25 ms 窗、10 ms 帧移和频谱通量，补充快速起音、点击、辅音和打击声。

正弦层采用全局安全缩放。瞬态和噪声使用两个独立的逐帧限幅总线，默认增益上限分别为 0.40 和 0.70。瞬态先使用可用峰值余量，再加入噪声；可能超过 0.88 时立即降低当前层，余量恢复后约 200 ms 平滑回升。这样不会因为某个噪声频带压低鼓点，也不会让一次强攻击控制整首音乐的残差音量。

噪声层会按频带连续跟踪状态，短暂低于阈值时保留轨迹两帧。变体由帧号和频带的确定性散列选择，并跳过上一变体；默认四变体不再每 200 ms 固定循环。相同输入和设置仍会生成相同结果，资源包体积不变。只使用一个变体时仍会重复；两个变体在避免相邻重复时只能交替，日常建议保留默认四个。100 ms 颗粒通过 50% 重叠窗交叉过渡，跟踪中的频带使用稍低的关闭阈值，减少门限附近的闪烁。

频谱平坦度不仅用于选择噪声频带，也会以 `flatness^0.25` 作为幅值置信度。接近随机噪声的镲片、齿音和环境纹理能获得较完整的幅值；偏音调、低平坦度的残差会自动减弱，不再把整段频带强行替换成合成噪声。

瞬态层使用双阈值滞回：超过高阈值才触发，回落到低阈值后才重新准备，并设有 50 ms 冷却和 100 ms 强制重置。候选频带还必须比前一短帧增长至少 2 dB，或新增能量达到当前能量的 35%。这些限制会合并同一次攻击里的重复峰，同时保留间隔明确的连续鼓点。

第一短窗通过峰均比额外检查局部强攻击，末窗移到录音内部，避免补零产生虚假瞬态。触发 tick 根据短窗中的攻击峰位置确定；必要时追加只含残差的尾帧，立体声两侧自动对齐。预览可能因此略长于输入，尾部颗粒会完整保留；检测并不能保证所有起音都被准确复现，时序仍受游戏 tick 限制。

`normal/high/experimental` 每帧额外最多使用 4/6/8 个噪声和 4/6/8 个瞬态组件；瞬态只在检测到起音时出现。

- `--no-hybrid-residual`：关闭通用残差，配合使用当前版本生成的纯正弦资源包。
- `--residual-variants`：每个频带的确定性噪声变体数，默认 4；资源包和转换命令必须相同。

### 频率与时间分辨率

- `--sample-rate`：采样率，默认 48000 Hz。
- `--grain-ms`：颗粒/分析窗长，默认 100 ms。
- `--hop-ms`：帧移，默认 50 ms。当前必须满足 `grain-ms = 2 * hop-ms`。
- `--min-frequency` / `--max-frequency`：分析与资源包的频率范围。
- `--frequency-grid adaptive|uniform`：默认 `adaptive`，按频段逐步增大间隔。
- `--frequency-step`：均匀网格的频点间距。显式提供该参数会选择 `uniform`；更小会减轻音高微调时的包络变化、改善相近峰的区分，但增加资源包体积和生成时间。
- `--phases`：离散相位数。更大会降低相位量化误差，但按比例扩大资源包。

正弦峰值在完整频谱中检测，通过四倍零填充与峰值插值估计实际频率；资源包只提供邻近基频，数据包以 `pitch` 微调播放频率。预览将同一个参数应用到载波和窗口时长，并校正平均重叠增益，保留变速后的完整尾音。这遵循音频播放速度随音高倍率变化的模型（见 [OpenAL 规范](https://www.openal.org/documentation/openal-1.1-specification.pdf)）；预览不模拟 OGG 编码误差或游戏重采样器的全部细节。相近峰共享一个资源包频点时只保留较强者，离散相位、稀疏分量预算和游戏调度仍会限制还原精度。

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

默认目标为 Minecraft Java 26.3：

- Resource Pack 格式 `97.1`
- Data Pack 格式 `121.0`
- 数据包函数目录 `data/<namespace>/function`

目标其他版本时，资源包使用 `--pack-format`，转换使用 `--data-pack-format`。旧版本如果要求 `functions` 复数目录，再为转换命令添加 `--layout legacy`。格式号必须查询目标 Minecraft 版本的官方发布说明，不要沿用 26.3 的默认值。

格式号依据 [Minecraft Java 26.3 官方发布说明](https://www.minecraft.net/en-us/article/minecraft-java-edition-26-3)。从 26.2 升级到 26.3 时，请重新生成资源包和数据包；GUI 会将旧目标版本或旧包格式显示为“参数不匹配”。仍需用于 26.2 时，显式指定资源包 `--pack-format 88.0` 和转换 `--data-pack-format 107.1`，目录继续使用 `modern`。

## 读取分析报告

`*_analysis.json` 中建议重点检查：

- `minecraft_version` 和 `data_pack.pack_format`：目标版本。
- `required_resource_pack`：实际需要的 namespace、频率银行、相位数和颗粒音量。
- `audio_config.frequency_grid`、`frequency_bands` 和 `frequency_count`：实际网格与资源数量。
- `quality_profile.band_budgets`：每个频段的硬分量预算。
- `component_model`：三层的平均与最大组件数量。
- `input_channels` / `output_channels` 和 `stereo`：实际声道数、是否保留输入立体声，以及游戏内左右声源偏移。
- `playback`：本曲独立计分板、监听标签、20 tick/s 时间轴长度、控制命令及按秒跳转用法；数据包内 `wav2mc-song.json` 包含相同信息。
- `video`：启用视频时记录展示实体屏幕、视频帧和控制信息，纯音频转换为 `null`；`audio_enabled` 表示是否输出音轨，静音视频的 `required_resource_pack` 为 `null`。
- `layer_scales.tone`：正弦层的全局安全缩放。
- `layer_scales.residual.noise` / `transient`：两个残差总线各自的 `gain_limit`、均值、最小值、最大值、P10 和 P90；`layer_scales.residual.release_ms` 是共同的恢复时间。
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
