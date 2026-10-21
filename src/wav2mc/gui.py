from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from collections.abc import Callable, Mapping
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import TypeVar

from .audio import MediaInfo, probe_media
from .bank import build_device_pack_set
from .config import (
    DEFAULT_DATA_PACK_FORMAT,
    DEFAULT_DEVICE_PACK_PROFILES,
    DEFAULT_LAYOUT,
    DEFAULT_MINECRAFT_VERSION,
    DEFAULT_RESOURCE_PACK_FORMAT,
    DEVICE_PROFILES,
    QUALITY_PROFILES,
    VIDEO_QUALITY_PROFILES,
    VIDEO_TILE_SIZE,
    AudioConfig,
    LoudnessCalibration,
    VideoConfig,
    device_audio_config,
    video_config_for_quality,
    video_renderer,
)
from .gui_state import (
    GuiSettings,
    inspect_device_pack,
    load_gui_settings,
    save_gui_settings,
)
from .pipeline import convert_audio
from .preview_player import PreviewPlayer
from .video import VideoInfo, probe_video, resolve_video_config
from .utils import (
    ProgressCallback, ProgressUpdate, TaskCancelled,
    conversion_output_paths, safe_namespace,
)


_T = TypeVar("_T")
MIN_GUI_GAIN_DB = -24.0
MAX_GUI_GAIN_DB = 12.0
PACK_STATE_TEXT = {
    "valid": "有效",
    "missing": "缺失",
    "mismatch": "参数不匹配",
}
STAGE_TEXT = {
    "decode": "解码",
    "video_decode": "解码视频",
    "video_resource_pack": "生成视频资源包",
    "analyse": "分析",
    "reconstruct": "重建预览",
    "datapack": "生成数据包",
    "report": "写入报告",
    "resource_pack": "生成资源包",
    "compress": "压缩",
}
GAME_COMMAND_LABELS = {
    "start": "开始播放",
    "pause": "暂停",
    "resume": "继续播放",
    "stop": "停止",
    "seek_forward": "前进 10 秒",
    "seek_backward": "后退 10 秒",
    "loop_on": "开启循环",
    "loop_off": "关闭循环",
    "status": "查看播放进度",
    "join": "加入收听",
    "leave": "退出收听",
    "start_all": "为所有在线玩家播放",
    "help": "查看播放帮助",
    "screen_create": "放置 / 重建屏幕",
    "screen_remove": "移除屏幕",
}


class _ToolTip:
    def __init__(self, widget: tk.Widget, text: Callable[[], str]) -> None:
        self.widget = widget
        self.text = text
        self.window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show, add=True)
        widget.bind("<Leave>", self._hide, add=True)

    def _show(self, _event: tk.Event[tk.Misc]) -> None:
        value = self.text().strip()
        if not value or self.window is not None:
            return
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(
            f"+{self.widget.winfo_rootx() + 12}+"
            f"{self.widget.winfo_rooty() + self.widget.winfo_height() + 6}"
        )
        tk.Label(
            self.window,
            text=value,
            justify=tk.LEFT,
            background="#202622",
            foreground="#ffffff",
            padx=8,
            pady=5,
            wraplength=560,
        ).pack()

    def _hide(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        if self.window is not None:
            self.window.destroy()
            self.window = None


class _AutoScrollableFrame(ttk.Frame):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(
            self,
            background="#ffffff",
            highlightthickness=0,
            borderwidth=0,
        )
        self.scrollbar = ttk.Scrollbar(
            self,
            orient=tk.VERTICAL,
            command=self.canvas.yview,
        )
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.body = ttk.Frame(self.canvas, style="Surface.TFrame")
        self._body_window = self.canvas.create_window(
            (0, 0),
            window=self.body,
            anchor=tk.NW,
        )
        self.body.bind("<Configure>", self._update_region)
        self.canvas.bind("<Configure>", self._resize_body)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    def _update_region(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        required = self.body.winfo_reqheight()
        available = self.canvas.winfo_height()
        if available > 1 and required > available + 16:
            self.scrollbar.grid(row=0, column=1, sticky="ns")
        else:
            self.scrollbar.grid_remove()
            self.canvas.yview_moveto(0.0)

    def _resize_body(self, event: tk.Event[tk.Misc]) -> None:
        self.canvas.itemconfigure(self._body_window, width=event.width)
        self._update_region()

    def _bind_wheel(self, _event: tk.Event[tk.Misc]) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)
        self.canvas.bind_all("<Button-4>", self._on_wheel)
        self.canvas.bind_all("<Button-5>", self._on_wheel)

    def _unbind_wheel(self, _event: tk.Event[tk.Misc]) -> None:
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")

    def _on_wheel(self, event: tk.Event[tk.Misc]) -> None:
        if self.body.winfo_reqheight() <= self.canvas.winfo_height():
            return
        if event.num == 4:
            direction = -1
        elif event.num == 5:
            direction = 1
        else:
            direction = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(direction, "units")


def mode_audio_config(mode: str) -> AudioConfig:
    try:
        profile = DEVICE_PROFILES[mode]
    except KeyError as exc:
        raise ValueError(f"Unknown mode: {mode}") from exc
    return device_audio_config(AudioConfig(), profile)


def gain_multiplier_from_db(gain_db: float) -> float:
    if not math.isfinite(gain_db):
        raise ValueError("Gain must be a finite dB value")
    return 10.0 ** (gain_db / 20.0)


def mode_maximum_command_load(mode: str, stereo: bool = True) -> int:
    profile = DEVICE_PROFILES[mode]
    quality = QUALITY_PROFILES[profile.quality_name]
    channel_count = 2 if stereo else 1
    return channel_count * 20 * (
        quality.max_components
        + quality.max_noise_components
        + quality.max_transient_components
    )


def mode_summary_fields(mode: str, stereo: bool = True) -> dict[str, str]:
    config = mode_audio_config(mode)
    actual_max = config.frequencies[-1]
    frequency_range = f"{config.min_frequency}-{config.max_frequency} Hz"
    if actual_max != config.max_frequency:
        frequency_range += f"（最高频点 {actual_max} Hz）"
    return {
        "frequency_range": frequency_range,
        "channels": "立体声" if stereo else "单声道",
        "command_load": f"≤ {mode_maximum_command_load(mode, stereo):,} 条/秒",
    }


def mode_summary(mode: str) -> str:
    config = mode_audio_config(mode)
    profile = DEVICE_PROFILES[mode]
    quality = QUALITY_PROFILES[profile.quality_name]
    actual_max = config.frequencies[-1]
    range_text = f"{config.min_frequency}-{config.max_frequency} Hz"
    if actual_max != config.max_frequency:
        range_text += f"（最高频点 {actual_max} Hz）"
    return (
        f"{range_text}  |  {len(config.frequencies)} 个频点  |  "
        f"{config.phase_count} 相位  |  最多 {quality.max_components} 个正弦 + "
        f"{quality.max_noise_components} 个噪声 + "
        f"{quality.max_transient_components} 个瞬态"
    )


def result_function_commands(report: Mapping[str, object]) -> dict[str, str]:
    playback = report.get("playback")
    commands = playback.get("commands") if isinstance(playback, Mapping) else None
    if isinstance(commands, Mapping):
        return {
            action: command
            for action in GAME_COMMAND_LABELS
            if isinstance(command := commands.get(action), str)
            and command.startswith("/function ")
        }
    namespace = safe_namespace(str(report.get("song_namespace") or "song"))
    return {"start": f"/function {namespace}:start"}


def result_function_command(report: Mapping[str, object], action: str = "start") -> str:
    commands = result_function_commands(report)
    if action not in commands:
        raise ValueError("此数据包没有提供该播放命令，请重新转换音频。")
    return commands[action]


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "未知"
    minutes, remaining = divmod(max(0.0, seconds), 60.0)
    if minutes >= 60:
        hours, minutes = divmod(int(minutes), 60)
        return f"{hours:d}:{minutes:02d}:{remaining:04.1f}"
    return f"{int(minutes):d}:{remaining:04.1f}"


def _compact_parent(path: Path, limit: int = 46) -> str:
    text = str(path.parent)
    if len(text) <= limit:
        return text
    return "…" + text[-(limit - 1) :]


class Wav2McApp:
    def __init__(
        self,
        root: tk.Tk,
        initial_input: Path | None = None,
        initial_output_dir: Path | None = None,
        settings_path: Path | None = None,
    ) -> None:
        self.root = root
        self.settings_path = settings_path
        self.settings = load_gui_settings(settings_path)
        self.busy = False
        self.close_pending = False
        self.destroyed = False
        self.cancel_event = threading.Event()
        self.event_queue: queue.Queue[tuple[object, ...]] = queue.Queue()
        self.probe_generation = 0
        self.media_info: MediaInfo | None = None
        self.media_info_path: Path | None = None
        self.video_info: VideoInfo | None = None
        self.video_info_path: Path | None = None
        self.last_outputs: Mapping[str, Path] | None = None
        self.last_report: Mapping[str, object] | None = None
        self.task_success_callback: Callable[[object], None] | None = None
        self.active_button: ttk.Button | None = None
        self.active_button_idle_text = ""
        self.details_open = False
        self.preview_player = PreviewPlayer()
        self.preview_after: str | None = None
        self.preview_dragging = False
        self.preview_error: str | None = None

        input_value = str(initial_input) if initial_input else ""
        output_value = (
            str(initial_output_dir)
            if initial_output_dir is not None
            else self.settings.output_dir
        )
        self.input_var = tk.StringVar(value=input_value)
        self.input_name_var = tk.StringVar(value=initial_input.name if initial_input else "未选择")
        self.input_parent_var = tk.StringVar(
            value=f"· {_compact_parent(initial_input)}" if initial_input else ""
        )
        self.output_var = tk.StringVar(value=output_value)
        self.song_name_var = tk.StringVar(
            value=initial_input.stem if initial_input else ""
        )
        self.audio_stream_var = tk.IntVar(value=0)
        self.stream_display_var = tk.StringVar(value="0 · 默认音轨")
        self.mode_var = tk.StringVar(value=self.settings.mode)
        self.gain_db_var = tk.DoubleVar(value=self.settings.gain_db)
        self.gain_text_var = tk.StringVar()
        self.masking_var = tk.BooleanVar(
            value=self.settings.psychoacoustic_masking
        )
        self.stereo_var = tk.BooleanVar(value=self.settings.preserve_stereo)
        self.applying_video_quality = False
        self.video_enabled_var = tk.BooleanVar(value=self.settings.video_enabled)
        self.video_quality_var = tk.StringVar(value=self.settings.video_quality)
        self.video_quality_display_var = tk.StringVar(
            value=self._video_quality_label(self.settings.video_quality)
        )
        self.video_width_var = tk.StringVar(value=str(self.settings.video_width) if self.settings.video_width is not None else "跟随源视频")
        self.video_height_var = tk.StringVar(value=str(self.settings.video_height) if self.settings.video_height is not None else "跟随源视频")
        self.video_fps_var = tk.StringVar(value=str(self.settings.video_fps))
        self.video_pixel_size_var = tk.StringVar(value=str(self.settings.video_pixel_size) if self.settings.video_pixel_size is not None else "自动")
        self.video_renderer_var = tk.StringVar(value=self.settings.video_renderer)
        self.video_renderer_display_var = tk.StringVar(value={"auto": "自动", "pixels": "逐像素", "texture": "纹理分块"}[self.settings.video_renderer])
        self.video_stream_var = tk.StringVar(value=str(self.settings.video_stream))
        self.include_audio_var = tk.BooleanVar(value=self.settings.include_audio)
        self.video_summary_var = tk.StringVar()
        self.result_video_var = tk.StringVar()
        self.advanced_open = self.settings.advanced_open
        self.bank_output_var = tk.StringVar(value=self.settings.bank_output_dir)
        self.profile_vars = {
            name: tk.BooleanVar(value=name in self.settings.selected_profiles)
            for name in DEFAULT_DEVICE_PACK_PROFILES
        }
        self.pack_status_vars = {
            name: tk.StringVar(value="检查中")
            for name in DEFAULT_DEVICE_PACK_PROFILES
        }
        self.media_info_var = tk.StringVar(value="等待选择媒体")
        self.summary_range_var = tk.StringVar()
        self.summary_channels_var = tk.StringVar()
        self.summary_load_var = tk.StringVar()
        self.convert_error_var = tk.StringVar()
        self.bank_error_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.result_summary_var = tk.StringVar()
        self.result_pack_var = tk.StringVar()
        self.preview_position_var = tk.DoubleVar(value=0.0)
        self.preview_time_var = tk.StringVar(value="0:00.0 / 0:00.0")

        self._configure_window()
        self._configure_styles()
        self._build_layout()
        self.mode_var.trace_add("write", self._update_mode_summary)
        self.stereo_var.trace_add("write", self._update_mode_summary)
        self.gain_db_var.trace_add("write", self._update_gain_text)
        self.bank_output_var.trace_add("write", self._bank_path_changed)
        self.video_quality_var.trace_add("write", self._video_quality_changed)
        for variable in (self.video_width_var, self.video_height_var, self.video_fps_var):
            variable.trace_add("write", self._video_dimensions_changed)
        for variable in (self.video_enabled_var, self.video_pixel_size_var):
            variable.trace_add("write", self._update_video_controls)
        self.video_renderer_var.trace_add("write", self._update_video_controls)
        self.video_stream_var.trace_add("write", self._video_stream_changed)
        self._update_video_controls()
        self._update_mode_summary()
        self._update_gain_text()
        self._refresh_pack_statuses()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Control-o>", lambda _event: self._choose_input())
        self.root.bind("<Control-Return>", lambda _event: self._primary_conversion_action())
        self.root.bind("<Escape>", lambda _event: self._request_cancel())
        self.root.after(50, self._poll_events)
        if initial_input is not None:
            self.root.after(20, lambda: self._begin_probe(initial_input))

    def _configure_window(self) -> None:
        self.root.title("wav2mc - Minecraft 音视频转换器")
        self.root.geometry("960x720")
        self.root.minsize(820, 640)
        self.root.configure(background="#eef1ee")

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        font = ("Noto Sans CJK SC", 9)
        style.configure(".", font=font, foreground="#202722")
        style.configure("TFrame", background="#eef1ee")
        style.configure("Surface.TFrame", background="#ffffff")
        style.configure("Surface.TLabel", background="#ffffff")
        style.configure(
            "Section.TLabel",
            background="#ffffff",
            foreground="#202722",
            font=(font[0], 10, "bold"),
        )
        style.configure(
            "Muted.TLabel",
            background="#eef1ee",
            foreground="#627069",
        )
        style.configure(
            "SurfaceMuted.TLabel",
            background="#ffffff",
            foreground="#66736d",
        )
        style.configure(
            "Meta.TLabel",
            background="#ffffff",
            foreground="#344039",
            font=(font[0], 8),
        )
        style.configure(
            "MetaMuted.TLabel",
            background="#ffffff",
            foreground="#68746e",
            font=(font[0], 8),
        )
        style.configure(
            "Result.TLabel",
            background="#ffffff",
            foreground="#26312b",
            font=(font[0], 8),
        )
        style.configure(
            "Error.TLabel",
            background="#ffffff",
            foreground="#a13c32",
        )
        style.configure(
            "Header.TLabel",
            background="#eef1ee",
            font=(font[0], 16, "bold"),
        )
        style.configure(
            "Version.TLabel",
            background="#dfe8e2",
            foreground="#315445",
            padding=(9, 4),
        )
        style.configure("TEntry", padding=5, fieldbackground="#ffffff")
        style.configure("TCombobox", padding=4, fieldbackground="#ffffff")
        style.configure("TSpinbox", padding=4, fieldbackground="#ffffff")
        style.configure("TButton", padding=(8, 5))
        style.configure(
            "Accent.TButton",
            background="#356b55",
            foreground="#ffffff",
            bordercolor="#2d5b49",
            padding=(12, 6),
        )
        style.map(
            "Accent.TButton",
            background=[("active", "#2d5d49"), ("disabled", "#9aaba3")],
            foreground=[("disabled", "#edf1ef")],
        )
        style.configure(
            "Mode.Toolbutton",
            padding=(8, 5),
            background="#edf1ee",
            bordercolor="#d2dad5",
        )
        style.map(
            "Mode.Toolbutton",
            background=[("selected", "#dbe9e1"), ("active", "#e3ebe6")],
            foreground=[("selected", "#244c3c")],
        )
        style.configure("Result.TButton", padding=(4, 1), font=(font[0], 7))
        style.configure("TNotebook", background="#eef1ee", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 8))
        style.map(
            "TNotebook.Tab",
            background=[("selected", "#ffffff"), ("!selected", "#dfe5e1")],
        )
        style.configure(
            "Horizontal.TProgressbar",
            background="#3f7c63",
            troughcolor="#dce3df",
        )

    def _build_layout(self) -> None:
        shell = ttk.Frame(self.root, padding=(14, 10, 14, 10))
        shell.grid(row=0, column=0, sticky="nsew")
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(1, weight=1)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        header = ttk.Frame(shell)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="wav2mc", style="Header.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text=f"Minecraft Java {DEFAULT_MINECRAFT_VERSION}",
            style="Version.TLabel",
        ).grid(row=0, column=1, sticky="e")

        self.notebook = ttk.Notebook(shell)
        self.notebook.grid(row=1, column=0, sticky="nsew")
        self.convert_tab = ttk.Frame(self.notebook, style="Surface.TFrame")
        self.bank_tab = ttk.Frame(self.notebook, style="Surface.TFrame")
        self.notebook.add(self.convert_tab, text="音频转换")
        self.notebook.add(self.bank_tab, text="资源包")
        self._build_convert_tab(self.convert_tab)
        self._build_bank_tab(self.bank_tab)

        activity = ttk.Frame(shell)
        activity.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        activity.columnconfigure(1, weight=1)
        ttk.Button(
            activity,
            text="任务详情",
            command=self._toggle_details,
        ).grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Label(activity, textvariable=self.status_var, style="Muted.TLabel").grid(
            row=0, column=1, sticky="w"
        )
        self.progress = ttk.Progressbar(
            activity,
            mode="determinate",
            maximum=100.0,
            variable=self.progress_var,
            length=220,
        )
        self.progress.grid(row=0, column=2, sticky="e")
        self.log = ScrolledText(
            activity,
            height=5,
            wrap=tk.WORD,
            font=("Noto Sans Mono CJK SC", 9),
            background="#ffffff",
            foreground="#26312b",
            borderwidth=1,
            relief=tk.SOLID,
            padx=8,
            pady=6,
            state=tk.DISABLED,
        )
        self.log.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(7, 0))
        self.log.grid_remove()

    def _section_heading(self, parent: ttk.Frame, text: str, row: int) -> None:
        ttk.Label(parent, text=text, style="Section.TLabel").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(0, 5)
        )
        ttk.Separator(parent).grid(
            row=row + 1, column=0, columnspan=3, sticky="ew", pady=(0, 8)
        )

    def _build_convert_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        self.convert_scroller = _AutoScrollableFrame(tab)
        self.convert_scroller.grid(row=0, column=0, sticky="nsew", padx=12, pady=(10, 4))
        workspace = self.convert_scroller.body
        workspace.columnconfigure(0, weight=1, uniform="convert")
        workspace.columnconfigure(2, weight=1, uniform="convert")
        workspace.rowconfigure(0, weight=1)
        workspace.bind("<Configure>", self._balance_conversion_columns)
        ttk.Separator(workspace, orient=tk.VERTICAL).grid(
            row=0, column=1, sticky="ns", padx=10
        )

        left = ttk.Frame(workspace, style="Surface.TFrame")
        left.grid(row=0, column=0, sticky="nsew")
        left.columnconfigure(1, weight=1)
        self._section_heading(left, "输入与输出", 0)
        ttk.Label(left, text="媒体文件", style="Surface.TLabel").grid(
            row=2, column=0, sticky="w", pady=4
        )
        self.input_entry = ttk.Entry(left, textvariable=self.input_var)
        self.input_entry.grid(row=2, column=1, sticky="ew", padx=(10, 6), pady=4)
        self.input_entry.bind("<Return>", self._probe_entry)
        self.input_entry.bind("<FocusOut>", self._probe_entry)
        ttk.Button(left, text="选择…", width=7, command=self._choose_input).grid(
            row=2, column=2, pady=4
        )
        _ToolTip(self.input_entry, self.input_var.get)

        selected = ttk.Frame(left, style="Surface.TFrame")
        selected.grid(row=3, column=0, columnspan=3, sticky="ew")
        selected.columnconfigure(0, weight=1)
        ttk.Label(
            selected,
            textvariable=self.input_name_var,
            style="Meta.TLabel",
        ).grid(row=0, column=0, sticky="w")
        parent_label = ttk.Label(
            selected,
            textvariable=self.input_parent_var,
            style="MetaMuted.TLabel",
        )
        parent_label.grid(row=0, column=1, sticky="w", padx=(7, 0))
        _ToolTip(parent_label, self.input_var.get)

        media = ttk.Frame(left, style="Surface.TFrame")
        media.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(4, 6))
        ttk.Label(media, textvariable=self.media_info_var, style="Meta.TLabel").grid(
            row=0, column=0, sticky="w"
        )

        ttk.Label(left, text="歌曲名称", style="Surface.TLabel").grid(
            row=5, column=0, sticky="w", pady=4
        )
        ttk.Entry(left, textvariable=self.song_name_var).grid(
            row=5, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=4
        )
        ttk.Label(left, text="输出目录", style="Surface.TLabel").grid(
            row=6, column=0, sticky="w", pady=4
        )
        output_entry = ttk.Entry(left, textvariable=self.output_var)
        output_entry.grid(row=6, column=1, sticky="ew", padx=(10, 6), pady=4)
        ttk.Button(
            left,
            text="选择…",
            width=7,
            command=lambda: self._choose_directory(self.output_var),
        ).grid(row=6, column=2, pady=4)
        _ToolTip(output_entry, self.output_var.get)

        self._section_heading(left, "视频", 8)
        ttk.Checkbutton(
            left,
            text="视频（展示实体）",
            variable=self.video_enabled_var,
        ).grid(row=10, column=0, columnspan=3, sticky="w", pady=(2, 5))
        video_options = ttk.Frame(left, style="Surface.TFrame")
        video_options.grid(row=11, column=0, columnspan=3, sticky="ew")
        self.video_widgets: list[tuple[ttk.Widget, str]] = []
        ttk.Label(video_options, text="画质", style="Surface.TLabel").grid(
            row=0, column=0, sticky="w", pady=3,
        )
        quality_box = ttk.Combobox(
            video_options, textvariable=self.video_quality_display_var,
            values=tuple(profile.label for profile in VIDEO_QUALITY_PROFILES.values()) + ("自定义",),
            state="readonly", width=9,
        )
        quality_box.grid(row=0, column=1, columnspan=3, sticky="w", padx=(8, 2), pady=3)
        quality_box.bind("<<ComboboxSelected>>", self._select_video_quality)
        ttk.Label(video_options, text="分辨率", style="Surface.TLabel").grid(
            row=1, column=0, sticky="w", pady=3
        )
        width_box = ttk.Spinbox(
            video_options, textvariable=self.video_width_var,
            from_=1, to=16384, width=7,
        )
        width_box.grid(row=1, column=1, padx=(8, 2), pady=3)
        ttk.Label(video_options, text="×", style="Surface.TLabel").grid(row=1, column=2)
        height_box = ttk.Spinbox(
            video_options, textvariable=self.video_height_var,
            from_=1, to=16384, width=7,
        )
        height_box.grid(row=1, column=3, padx=(2, 8), pady=3)
        fps_box = ttk.Combobox(
            video_options, textvariable=self.video_fps_var,
            values=(1, 2, 4, 5, 10, 20), width=4, state="readonly",
        )
        fps_box.grid(row=1, column=4, pady=3)
        ttk.Label(video_options, text="fps", style="Surface.TLabel").grid(
            row=1, column=5, padx=(3, 0)
        )
        ttk.Label(video_options, text="像素边长", style="Surface.TLabel").grid(
            row=2, column=0, sticky="w", pady=3
        )
        pixel_box = ttk.Spinbox(
            video_options, textvariable=self.video_pixel_size_var,
            from_=0.000001, to=2.0, increment=0.001, width=7,
        )
        pixel_box.grid(row=2, column=1, padx=(8, 2), pady=3)
        ttk.Label(video_options, text="格", style="Surface.TLabel").grid(row=2, column=2)
        ttk.Label(video_options, text="视频流", style="Surface.TLabel").grid(
            row=2, column=3, padx=(4, 0)
        )
        stream_box = ttk.Spinbox(
            video_options, textvariable=self.video_stream_var,
            from_=0, to=99, width=4,
        )
        stream_box.grid(row=2, column=4, pady=3)
        audio_box = ttk.Checkbutton(
            video_options, text="保留音频", variable=self.include_audio_var,
        )
        ttk.Label(video_options, text="渲染", style="Surface.TLabel").grid(row=3, column=0, sticky="w", pady=3)
        renderer_box = ttk.Combobox(
            video_options, textvariable=self.video_renderer_display_var,
            values=("自动", "逐像素", "纹理分块"), state="readonly", width=9,
        )
        renderer_box.grid(row=3, column=1, columnspan=3, sticky="w", padx=(8, 2), pady=3)
        renderer_box.bind("<<ComboboxSelected>>", self._select_video_renderer)
        audio_box.grid(row=4, column=0, columnspan=6, sticky="w", pady=3)
        self.video_dimension_widgets = (width_box, height_box)
        self.video_widgets.extend((
            (quality_box, "readonly"),
            (width_box, tk.NORMAL), (height_box, tk.NORMAL),
            (fps_box, "readonly"), (pixel_box, tk.NORMAL),
            (stream_box, tk.NORMAL), (audio_box, tk.NORMAL),
            (renderer_box, "readonly"),
        ))
        ttk.Label(
            left, textvariable=self.video_summary_var,
            style="MetaMuted.TLabel", wraplength=320,
        ).grid(row=12, column=0, columnspan=3, sticky="w", pady=(4, 0))

        right = ttk.Frame(workspace, style="Surface.TFrame")
        right.grid(row=0, column=2, sticky="nsew")
        right.columnconfigure(0, weight=1)
        self._section_heading(right, "转换设置", 0)
        modes = ttk.Frame(right, style="Surface.TFrame")
        modes.grid(row=2, column=0, sticky="ew", pady=(0, 9))
        mode_labels = {
            "voice": "语音",
            "normal": "普通",
            "high": "高质",
            "experimental": "实验",
        }
        for column, mode in enumerate(DEFAULT_DEVICE_PACK_PROFILES):
            modes.columnconfigure(column, weight=1, uniform="mode")
            button = ttk.Radiobutton(
                modes,
                text=mode_labels[mode],
                value=mode,
                variable=self.mode_var,
                style="Mode.Toolbutton",
            )
            button.grid(
                row=0,
                column=column,
                sticky="ew",
                padx=(0 if column == 0 else 3, 0),
            )
            _ToolTip(button, lambda value=mode: value)

        gain_header = ttk.Frame(right, style="Surface.TFrame")
        gain_header.grid(row=3, column=0, sticky="ew")
        gain_header.columnconfigure(1, weight=1)
        ttk.Label(gain_header, text="转换增益", style="Surface.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            gain_header,
            textvariable=self.gain_text_var,
            style="SurfaceMuted.TLabel",
        ).grid(row=0, column=1, sticky="e", padx=8)
        ttk.Spinbox(
            gain_header,
            from_=MIN_GUI_GAIN_DB,
            to=MAX_GUI_GAIN_DB,
            increment=0.5,
            format="%.1f",
            width=6,
            justify=tk.RIGHT,
            textvariable=self.gain_db_var,
        ).grid(row=0, column=2, sticky="e")
        ttk.Label(gain_header, text="dB", style="Surface.TLabel").grid(
            row=0, column=3, padx=(4, 7)
        )
        gain_controls = ttk.Frame(right, style="Surface.TFrame")
        gain_controls.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        gain_controls.columnconfigure(0, weight=1)
        self.gain_scale = tk.Scale(
            gain_controls,
            from_=MIN_GUI_GAIN_DB,
            to=MAX_GUI_GAIN_DB,
            resolution=0.5,
            orient=tk.HORIZONTAL,
            showvalue=False,
            variable=self.gain_db_var,
            background="#ffffff",
            troughcolor="#dce3df",
            activebackground="#356b55",
            highlightthickness=0,
            borderwidth=0,
            sliderlength=14,
            width=10,
        )
        self.gain_scale.grid(row=0, column=0, sticky="ew")
        reset_button = ttk.Button(
            gain_controls,
            text="↺",
            width=2,
            command=lambda: self.gain_db_var.set(0.0),
        )
        reset_button.grid(row=0, column=1, padx=(5, 0))
        _ToolTip(reset_button, lambda: "增益归零")

        summary = ttk.Frame(right, style="Surface.TFrame")
        summary.grid(row=5, column=0, sticky="ew", pady=(5, 3))
        summary.columnconfigure(1, weight=1)
        for row, (label, variable) in enumerate(
            (
                ("频率范围", self.summary_range_var),
                ("输出声道", self.summary_channels_var),
                ("最大命令负载", self.summary_load_var),
            )
        ):
            ttk.Label(summary, text=label, style="SurfaceMuted.TLabel").grid(
                row=row, column=0, sticky="w", pady=2
            )
            ttk.Label(summary, textvariable=variable, style="Surface.TLabel").grid(
                row=row, column=1, sticky="e", pady=2
            )

        self.advanced_frame = ttk.Frame(right, style="Surface.TFrame")
        self.advanced_frame.grid(row=6, column=0, sticky="ew", pady=(6, 0))
        self.advanced_frame.columnconfigure(1, weight=1)
        ttk.Label(self.advanced_frame, text="音轨", style="Surface.TLabel").grid(
            row=0, column=0, sticky="w", pady=3
        )
        self.stream_box = ttk.Combobox(
            self.advanced_frame,
            textvariable=self.stream_display_var,
            values=("0 · 默认音轨",),
            state="readonly",
        )
        self.stream_box.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=3)
        self.stream_box.bind("<<ComboboxSelected>>", self._stream_selected)
        ttk.Checkbutton(
            self.advanced_frame,
            text="保留立体声",
            variable=self.stereo_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Checkbutton(
            self.advanced_frame,
            text="启用心理声学掩蔽",
            variable=self.masking_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=3)
        action_row = ttk.Frame(tab, style="Surface.TFrame")
        self.convert_action_row = action_row
        action_row.grid(row=1, column=0, sticky="ew", padx=12, pady=(4, 7))
        action_row.columnconfigure(1, weight=1)
        ttk.Label(
            action_row,
            textvariable=self.convert_error_var,
            style="Error.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self.advanced_button = ttk.Button(
            action_row,
            command=self._toggle_advanced,
        )
        self.advanced_button.grid(row=0, column=2, padx=(8, 8))
        ttk.Button(
            action_row,
            text="打开输出目录",
            command=lambda: self._open_path(Path(self.output_var.get() or "output"), True),
        ).grid(row=0, column=3, padx=(0, 8))
        self.convert_button = ttk.Button(
            action_row,
            text="开始转换",
            style="Accent.TButton",
            command=self._primary_conversion_action,
        )
        self.convert_button.grid(row=0, column=4)
        self._set_advanced(self.advanced_open)

        self.result_frame = ttk.Frame(tab, style="Surface.TFrame", padding=(4, 2))
        self.result_frame.grid(row=1, column=0, sticky="ew")
        self.result_frame.columnconfigure(0, weight=1)
        ttk.Label(
            self.result_frame,
            textvariable=self.result_summary_var,
            style="Result.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            self.result_frame,
            textvariable=self.result_pack_var,
            style="MetaMuted.TLabel",
        ).grid(row=0, column=1, sticky="w", padx=(2, 0))
        self.pack_jump_button = ttk.Button(
            self.result_frame,
            text="生成",
            width=4,
            style="Result.TButton",
            command=self._jump_to_required_pack,
        )
        self.pack_jump_button.grid(row=0, column=2, sticky="w", padx=(2, 0))
        _ToolTip(self.pack_jump_button, lambda: "生成匹配资源包")
        self.preview_play_button = ttk.Button(
            self.result_frame,
            text="试听",
            width=4,
            style="Result.TButton",
            command=self._play_preview,
        )
        self.preview_play_button.grid(row=0, column=3, padx=(2, 0))
        _ToolTip(self.preview_play_button, lambda: "试听 / 暂停本地音轨预览，视频画面在 Minecraft 中播放")
        directory_button = ttk.Button(
            self.result_frame,
            text="目录",
            width=4,
            style="Result.TButton",
            command=self._open_result_directory,
        )
        directory_button.grid(row=0, column=4, padx=(2, 0))
        _ToolTip(directory_button, lambda: "打开输出目录")
        copy_button = ttk.Menubutton(
            self.result_frame,
            text="命令 ▾",
            width=6,
            style="Result.TButton",
        )
        self.command_menu = tk.Menu(copy_button, tearoff=False)
        copy_button.configure(menu=self.command_menu)
        self._refresh_command_menu()
        copy_button.grid(row=0, column=5, padx=(2, 0))
        _ToolTip(copy_button, lambda: "复制游戏内播放命令，在 Minecraft 中执行")
        ttk.Button(
            self.result_frame,
            text="×",
            width=2,
            style="Result.TButton",
            command=self._dismiss_result,
        ).grid(row=0, column=6, sticky="e", padx=(2, 0))
        preview_controls = ttk.Frame(self.result_frame, style="Surface.TFrame")
        preview_controls.grid(row=1, column=0, columnspan=7, sticky="ew", pady=(3, 1))
        preview_controls.columnconfigure(1, weight=1)
        self.preview_stop_button = ttk.Button(
            preview_controls,
            text="停止试听",
            width=8,
            style="Result.TButton",
            command=self._stop_preview,
        )
        self.preview_stop_button.grid(row=0, column=0, padx=(0, 5))
        self.preview_seek = ttk.Scale(
            preview_controls,
            variable=self.preview_position_var,
            from_=0.0,
            to=1.0,
            command=self._preview_seek_changed,
        )
        self.preview_seek.grid(row=0, column=1, sticky="ew")
        self.preview_seek.bind("<ButtonPress-1>", self._begin_preview_seek)
        self.preview_seek.bind("<ButtonRelease-1>", self._commit_preview_seek)
        self.preview_seek.bind("<KeyRelease>", self._commit_preview_seek)
        ttk.Label(
            preview_controls,
            textvariable=self.preview_time_var,
            style="MetaMuted.TLabel",
        ).grid(row=0, column=2, padx=5)
        ttk.Button(
            preview_controls,
            text="系统播放器",
            style="Result.TButton",
            command=self._open_external_preview,
        ).grid(row=0, column=3)
        self._update_preview_controls()
        self.result_video_label = ttk.Label(
            self.result_frame, textvariable=self.result_video_var,
            style="MetaMuted.TLabel",
        )
        self.result_video_label.grid(
            row=2, column=0, columnspan=7, sticky="w", pady=(2, 0),
        )
        self.result_video_label.grid_remove()
        self.result_frame.grid_remove()

    def _build_bank_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        header = ttk.Frame(tab, style="Surface.TFrame", padding=(18, 16, 18, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="资源包目录", style="Surface.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        bank_entry = ttk.Entry(header, textvariable=self.bank_output_var)
        bank_entry.grid(row=0, column=1, sticky="ew", padx=(12, 6))
        ttk.Button(
            header,
            text="选择…",
            command=lambda: self._choose_directory(self.bank_output_var),
        ).grid(row=0, column=2)
        _ToolTip(bank_entry, self.bank_output_var.get)

        table = ttk.Frame(tab, style="Surface.TFrame", padding=(18, 8))
        table.grid(row=1, column=0, sticky="ew")
        table.columnconfigure(0, weight=1)
        table.columnconfigure(1, weight=2)
        table.columnconfigure(2, weight=2)
        table.columnconfigure(3, weight=1)
        for column, text in enumerate(("档位", "频率范围", "每帧预算", "状态")):
            ttk.Label(table, text=text, style="SurfaceMuted.TLabel").grid(
                row=0, column=column, sticky="w", pady=(0, 6)
            )
        for row, name in enumerate(DEFAULT_DEVICE_PACK_PROFILES, start=1):
            config = mode_audio_config(name)
            quality = QUALITY_PROFILES[DEVICE_PROFILES[name].quality_name]
            ttk.Checkbutton(
                table,
                text=name,
                variable=self.profile_vars[name],
            ).grid(row=row, column=0, sticky="w", pady=7)
            ttk.Label(
                table,
                text=f"{config.min_frequency}-{config.max_frequency} Hz",
                style="Surface.TLabel",
            ).grid(row=row, column=1, sticky="w", pady=7)
            ttk.Label(
                table,
                text=(
                    f"{quality.max_components} 正弦 + "
                    f"{quality.max_noise_components} 噪声 + "
                    f"{quality.max_transient_components} 瞬态"
                ),
                style="Surface.TLabel",
            ).grid(row=row, column=2, sticky="w", pady=7)
            ttk.Label(
                table,
                textvariable=self.pack_status_vars[name],
                style="Surface.TLabel",
            ).grid(row=row, column=3, sticky="w", pady=7)
            ttk.Separator(table).grid(
                row=row + 4, column=0, columnspan=4, sticky="ew"
            )

        actions = ttk.Frame(tab, style="Surface.TFrame", padding=(18, 8, 18, 14))
        actions.grid(row=3, column=0, sticky="ew")
        actions.columnconfigure(1, weight=1)
        ttk.Label(actions, textvariable=self.bank_error_var, style="Error.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(
            actions,
            text="打开资源包目录",
            command=lambda: self._open_path(
                Path(self.bank_output_var.get() or "output/device_banks"), True
            ),
        ).grid(row=0, column=2, padx=(8, 8))
        self.bank_button = ttk.Button(
            actions,
            text="生成选中",
            style="Accent.TButton",
            command=self._primary_bank_action,
        )
        self.bank_button.grid(row=0, column=3)

    def _balance_conversion_columns(self, event: tk.Event[tk.Misc]) -> None:
        pane_width = max(260, (event.width - 28) // 2)
        self.convert_scroller.body.columnconfigure(0, minsize=pane_width)
        self.convert_scroller.body.columnconfigure(2, minsize=pane_width)

    def _probe_entry(self, _event: tk.Event[tk.Misc]) -> None:
        path = Path(self.input_var.get()).expanduser()
        if path.is_file():
            self._set_input_display(path)
            self._begin_probe(path)

    def _choose_input(self) -> None:
        if self.input_var.get():
            initial_dir = Path(self.input_var.get()).expanduser().parent
        elif self.settings.last_input_dir:
            initial_dir = Path(self.settings.last_input_dir).expanduser()
        else:
            initial_dir = Path.cwd()
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="选择音频或视频文件",
            initialdir=str(initial_dir),
            filetypes=(
                (
                    "音频与视频",
                    "*.wav *.mp3 *.flac *.m4a *.m4s *.aac *.ogg "
                    "*.opus *.aiff *.mp4 *.mkv *.webm *.mov *.avi *.mpeg *.mpg *.gif",
                ),
                ("所有文件", "*"),
            ),
        )
        if not selected:
            return
        path = Path(selected)
        previous_stem = Path(self.input_var.get()).stem if self.input_var.get() else ""
        self.input_var.set(str(path))
        self._set_input_display(path)
        if not self.song_name_var.get().strip() or self.song_name_var.get() == previous_stem:
            self.song_name_var.set(path.stem)
        self._begin_probe(path)

    def _set_input_display(self, path: Path) -> None:
        self.input_name_var.set(path.name)
        self.input_parent_var.set(f"· {_compact_parent(path)}")

    def _choose_directory(self, variable: tk.StringVar) -> None:
        initial = Path(variable.get()).expanduser() if variable.get() else Path.cwd()
        selected = filedialog.askdirectory(
            parent=self.root,
            title="选择输出目录",
            initialdir=str(initial),
        )
        if selected:
            variable.set(selected)
            if variable is self.bank_output_var:
                self._refresh_pack_statuses()

    def _begin_probe(self, path: Path) -> None:
        self.probe_generation += 1
        generation = self.probe_generation
        self.media_info = None
        self.media_info_path = None
        self.video_info = None
        self.video_info_path = None
        self.media_info_var.set("正在探测…")
        self.audio_stream_var.set(0)
        self.stream_display_var.set("0 · 默认音轨")
        self.stream_box.configure(values=("0 · 默认音轨",), state="readonly")
        self._update_video_controls()
        try:
            video_stream = int(self.video_stream_var.get())
        except (tk.TclError, ValueError):
            video_stream = 0

        def runner() -> None:
            video = None
            try:
                try:
                    info = probe_media(path)
                except RuntimeError as exc:
                    if not str(exc).startswith("No audio stream found"):
                        raise
                    video = probe_video(path) if video_stream == 0 else probe_video(path, video_stream)
                    info = MediaInfo(duration=video.duration, format_name="video", streams=())
            except Exception as exc:
                self.event_queue.put(("probe_error", generation, path, exc))
            else:
                self.event_queue.put(("probe_success", generation, path, info))
                if video is None:
                    try:
                        video = probe_video(path) if video_stream == 0 else probe_video(path, video_stream)
                    except (OSError, RuntimeError, ValueError):
                        pass
                if video is not None:
                    self.event_queue.put(("video_probe_success", generation, path, video, video_stream))

        threading.Thread(target=runner, name="wav2mc-media-probe", daemon=True).start()

    def _video_stream_changed(self, *_args: object) -> None:
        self.video_info = None
        self.video_info_path = None
        self._update_video_controls()
        path = Path(self.input_var.get()).expanduser()
        if not path.is_file():
            return
        try:
            stream = int(self.video_stream_var.get())
            if stream < 0:
                return
        except (tk.TclError, ValueError):
            return
        generation = self.probe_generation

        def runner() -> None:
            try:
                info = probe_video(path) if stream == 0 else probe_video(path, stream)
            except (OSError, RuntimeError, ValueError):
                return
            self.event_queue.put(("video_probe_success", generation, path, info, stream))

        threading.Thread(target=runner, name="wav2mc-video-probe", daemon=True).start()

    def _video_probe_succeeded(
        self, generation: int, path: Path, info: VideoInfo, stream: int = 0,
    ) -> None:
        try:
            current_stream = int(self.video_stream_var.get())
        except (tk.TclError, ValueError):
            return
        if generation != self.probe_generation or self.input_var.get() != str(path) or stream != current_stream:
            return
        self.video_info = info
        self.video_info_path = path
        self._update_video_controls()

    def _probe_succeeded(self, generation: int, path: Path, info: MediaInfo) -> None:
        if generation != self.probe_generation or self.input_var.get() != str(path):
            return
        self.media_info = info
        self.media_info_path = path
        if not info.streams:
            self.audio_stream_var.set(0)
            self.stream_display_var.set("无音轨")
            self.stream_box.configure(values=(), state=tk.DISABLED)
            self._update_silent_media_description()
            return
        stream = info.streams[0]
        self.media_info_var.set(
            f"{_format_duration(info.duration)} · {stream.codec.upper()} · "
            f"{stream.sample_rate / 1000:g}k · {stream.channels}ch"
        )
        values = tuple(
            f"{item.audio_index} · {item.codec.upper()} · "
            f"{item.sample_rate:,} Hz · {item.channels} 声道"
            for item in info.streams
        )
        self.stream_box.configure(values=values, state="readonly")
        self.stream_display_var.set(values[0])
        self.audio_stream_var.set(0)

    def _probe_failed(self, generation: int, path: Path, error: Exception) -> None:
        if generation != self.probe_generation or self.input_var.get() != str(path):
            return
        self.media_info_var.set("探测失败 · 使用音轨 0")
        self._append_log(f"媒体探测失败：{error}")

    def _update_silent_media_description(self) -> None:
        if self.media_info is None or self.media_info.streams:
            return
        action = "无声视频" if self.video_enabled_var.get() else "启用视频转换"
        self.media_info_var.set(
            f"{_format_duration(self.media_info.duration)} · 无音轨 · {action}"
        )

    def _video_config(self) -> VideoConfig | None:
        if not self.video_enabled_var.get():
            return None
        native = self.video_quality_var.get() == "native"
        pixel_text = self.video_pixel_size_var.get().strip()
        config = VideoConfig(
            width=None if native else int(self.video_width_var.get()),
            height=None if native else int(self.video_height_var.get()),
            fps=int(self.video_fps_var.get()),
            pixel_size=None if native and pixel_text in ("", "自动", "None") else float(pixel_text),
            renderer=self.video_renderer_var.get(), native_resolution=native,
        )
        config.validate()
        return config

    @staticmethod
    def _video_quality_label(quality: str | None) -> str:
        profile = VIDEO_QUALITY_PROFILES.get(quality)
        return profile.label if profile is not None else "自定义"

    def _select_video_quality(self, _event: tk.Event[tk.Misc]) -> None:
        selected = self.video_quality_display_var.get()
        quality = next(
            (name for name, profile in VIDEO_QUALITY_PROFILES.items() if profile.label == selected),
            "custom",
        )
        self.video_quality_var.set(quality)

    def _select_video_renderer(self, _event: tk.Event[tk.Misc]) -> None:
        renderer = {"自动": "auto", "逐像素": "pixels", "纹理分块": "texture"}.get(
            self.video_renderer_display_var.get(), "auto",
        )
        self.video_renderer_var.set(renderer)

    def _video_quality_changed(self, *_args: object) -> None:
        quality = self.video_quality_var.get()
        self.video_quality_display_var.set(self._video_quality_label(quality))
        if self.applying_video_quality:
            return
        self.applying_video_quality = True
        try:
            if quality in VIDEO_QUALITY_PROFILES:
                config = video_config_for_quality(quality)
                self.video_width_var.set(str(config.width) if config.width is not None else "跟随源视频")
                self.video_height_var.set(str(config.height) if config.height is not None else "跟随源视频")
                self.video_fps_var.set(str(config.fps))
                self.video_pixel_size_var.set(str(config.pixel_size) if config.pixel_size is not None else "自动")
            elif quality == "custom":
                try:
                    int(self.video_width_var.get())
                    int(self.video_height_var.get())
                except (tk.TclError, ValueError):
                    default = video_config_for_quality()
                    current = self.video_info if self.video_info_path == Path(self.input_var.get()).expanduser() else None
                    self.video_width_var.set(str(current.display_width if current else default.width))
                    self.video_height_var.set(str(current.display_height if current else default.height))
                if self.video_pixel_size_var.get() in ("", "自动", "None"):
                    self.video_pixel_size_var.set(str(3.2 / int(self.video_width_var.get())))
        finally:
            self.applying_video_quality = False
        self._update_video_controls()

    def _video_dimensions_changed(self, *_args: object) -> None:
        if self.applying_video_quality:
            return
        profile = VIDEO_QUALITY_PROFILES.get(self.video_quality_var.get())
        if profile is not None and profile.name != "native":
            try:
                dimensions = (
                    int(self.video_width_var.get()), int(self.video_height_var.get()),
                    int(self.video_fps_var.get()),
                )
            except (tk.TclError, ValueError):
                dimensions = None
            if dimensions != (profile.width, profile.height, profile.fps):
                self.video_quality_var.set("custom")
        self._update_video_controls()

    def _update_video_controls(self, *_args: object) -> None:
        if self.applying_video_quality:
            return
        enabled = self.video_enabled_var.get()
        native = self.video_quality_var.get() == "native"
        self._update_silent_media_description()
        for widget, normal_state in self.video_widgets:
            widget.configure(state=normal_state if enabled else tk.DISABLED)
        for widget in self.video_dimension_widgets:
            widget.configure(state=tk.DISABLED if native or not enabled else tk.NORMAL)
        self.video_renderer_display_var.set({"auto": "自动", "pixels": "逐像素", "texture": "纹理分块"}.get(self.video_renderer_var.get(), "自动"))
        if not enabled:
            self.video_summary_var.set("开启后生成展示实体屏幕，高分辨率输出配套视频资源包。")
            return
        try:
            config = self._video_config()
            assert config is not None
            if native:
                self.applying_video_quality = True
                try:
                    valid_info = self.video_info is not None and self.video_info_path == Path(self.input_var.get()).expanduser()
                    self.video_width_var.set(str(self.video_info.display_width) if valid_info else "跟随源视频")
                    self.video_height_var.set(str(self.video_info.display_height) if valid_info else "跟随源视频")
                finally:
                    self.applying_video_quality = False
                if not valid_info:
                    self.video_summary_var.set(
                        "原分辨率 · 跟随源视频，读取后显示尺寸与负载\n"
                        "纹理分块 · 需启用生成的视频资源包"
                    )
                    return
                config = resolve_video_config(config, self.video_info)
            assert config.width is not None and config.height is not None and config.pixel_size is not None
            renderer = video_renderer(config)
            pixels = config.width * config.height
            count = pixels if renderer == "pixels" else math.ceil(config.width / VIDEO_TILE_SIZE) * math.ceil(config.height / VIDEO_TILE_SIZE)
            renderer_text = "逐像素" if renderer == "pixels" else "纹理分块 · 需视频资源包"
            memory = ""
            duration = (
                self.video_info.duration
                if self.video_info is not None
                and self.video_info_path == Path(self.input_var.get()).expanduser()
                else None
            )
            if renderer == "texture" and duration:
                mib = math.ceil(duration * config.fps) * count * VIDEO_TILE_SIZE ** 2 * 4 / 1024 ** 2
                memory = f" · 图集约 {mib:g} MiB"
            self.video_summary_var.set(
                f"{self._video_quality_label(self.video_quality_var.get())} · {config.width}×{config.height} · {count:,} 个显示实体\n"
                f"{count * config.fps:,} 次更新/秒 · {renderer_text}{memory}\n"
                f"{config.width * config.pixel_size:g} × {config.height * config.pixel_height:g} 格屏幕"
            )
        except (tk.TclError, ValueError, TypeError):
            self.video_summary_var.set("视频参数无效；逐像素模式最多 4096 个实体，可改用纹理分块。")

    def _stream_selected(self, _event: tk.Event[tk.Misc]) -> None:
        value = self.stream_display_var.get().partition("·")[0].strip()
        try:
            self.audio_stream_var.set(int(value))
        except ValueError:
            self.audio_stream_var.set(0)

    def _update_mode_summary(self, *_args: object) -> None:
        try:
            fields = mode_summary_fields(self.mode_var.get(), self.stereo_var.get())
        except (tk.TclError, ValueError, KeyError):
            self.summary_range_var.set("无效")
            self.summary_channels_var.set("无效")
            self.summary_load_var.set("无效")
            return
        self.summary_range_var.set(fields["frequency_range"])
        self.summary_channels_var.set(fields["channels"])
        self.summary_load_var.set(fields["command_load"])

    def _update_gain_text(self, *_args: object) -> None:
        try:
            value = float(self.gain_db_var.get())
            multiplier = gain_multiplier_from_db(value)
        except (tk.TclError, ValueError):
            self.gain_text_var.set("无效")
        else:
            self.gain_text_var.set(f"{multiplier:.2f}x")

    def _toggle_advanced(self) -> None:
        self._set_advanced(not self.advanced_open)

    def _set_advanced(self, opened: bool) -> None:
        self.advanced_open = opened
        self.advanced_button.configure(text="高级设置 ▾" if opened else "高级设置 ▸")
        if opened:
            self.advanced_frame.grid()
        else:
            self.advanced_frame.grid_remove()
        self.root.after_idle(self.convert_scroller._update_region)

    def _primary_conversion_action(self) -> None:
        if self.busy:
            self._request_cancel()
        else:
            self._start_conversion()

    def _start_conversion(self) -> None:
        self.convert_error_var.set("")
        source = Path(self.input_var.get()).expanduser()
        if not source.is_file():
            self.convert_error_var.set("请选择存在的音频或媒体文件。")
            return
        if (
            self.media_info is not None and self.media_info_path == source
            and not self.media_info.streams
            and not self.video_enabled_var.get()
        ):
            self.convert_error_var.set("此媒体没有音轨，请开启视频（展示实体）转换。")
            return
        output_text = self.output_var.get().strip()
        if not output_text:
            self.convert_error_var.set("请选择输出目录。")
            return
        output_dir = Path(output_text).expanduser()
        song_name = self.song_name_var.get().strip() or source.stem
        try:
            stream = self.audio_stream_var.get()
            gain_db = float(self.gain_db_var.get())
            mode = self.mode_var.get()
            config = mode_audio_config(mode)
            video_config = self._video_config()
            if (
                video_config is not None and video_config.native_resolution
                and self.video_info is not None and self.video_info_path == source
            ):
                resolve_video_config(video_config, self.video_info)
            video_stream = int(self.video_stream_var.get()) if video_config else 0
        except (tk.TclError, ValueError) as exc:
            self.convert_error_var.set(f"参数无效：{exc}")
            return
        if not MIN_GUI_GAIN_DB <= gain_db <= MAX_GUI_GAIN_DB:
            self.convert_error_var.set("转换增益必须在 -24 到 +12 dB 之间。")
            return
        if stream < 0:
            self.convert_error_var.set("音轨索引不能为负数。")
            return
        if video_stream < 0:
            self.convert_error_var.set("视频流索引不能为负数。")
            return

        targets = conversion_output_paths(
            output_dir, song_name,
            include_video_resource_pack=video_config is not None and video_renderer(video_config) == "texture",
        )
        existing = [path for path in targets.values() if path.exists()]
        if existing and not messagebox.askyesno(
            "覆盖输出",
            f"已有 {len(existing)} 个同名输出文件，是否覆盖？",
            parent=self.root,
        ):
            return

        profile = DEVICE_PROFILES[mode]
        quality = QUALITY_PROFILES[profile.quality_name]
        gain = gain_multiplier_from_db(gain_db)
        masking = self.masking_var.get()
        preserve_stereo = self.stereo_var.get()
        include_audio = self.include_audio_var.get() if video_config else True
        preview_bank = inspect_device_pack(
            Path(self.bank_output_var.get() or "output/device_banks").expanduser(), mode,
        )

        def work(
            progress_callback: ProgressCallback,
            cancel_check: Callable[[], bool],
        ) -> Mapping[str, Path]:
            return convert_audio(
                source=source,
                output_dir=output_dir,
                song_name=song_name,
                config=config,
                quality=quality,
                data_pack_format=DEFAULT_DATA_PACK_FORMAT,
                layout=DEFAULT_LAYOUT,
                bank_namespace=f"wav2mc_{mode}",
                category="record",
                requested_gain=gain,
                bank_grain_level=1.0,
                loudness_calibration=LoudnessCalibration(),
                psychoacoustic_masking=masking,
                device_profile=mode,
                audio_stream=stream,
                preserve_stereo=preserve_stereo,
                preview_resource_pack=preview_bank.path if preview_bank.state == "valid" else None,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                video_config=video_config,
                video_stream=video_stream,
                include_audio=include_audio,
            )

        self._stop_preview()
        self._persist_settings()
        self.result_frame.grid_remove()
        self.convert_action_row.grid()
        self._run_task(
            f"正在转换 {source.name}",
            work,
            self._conversion_finished,
            self.convert_button,
            "开始转换",
        )

    def _conversion_finished(self, result: object) -> None:
        outputs = result
        if not isinstance(outputs, Mapping):
            raise TypeError("Conversion returned invalid outputs")
        typed_outputs = {str(key): Path(value) for key, value in outputs.items()}
        report = json.loads(typed_outputs["report"].read_text(encoding="utf-8"))
        self._stop_preview()
        self.last_outputs = typed_outputs
        self.last_report = report
        self._refresh_command_menu()
        try:
            self.preview_player.load(typed_outputs["preview"])
        except (OSError, ValueError, RuntimeError) as exc:
            self._append_log(f"无法读取试听文件：{exc}")
        self._update_preview_controls()
        duration = float(report.get("input_duration_seconds", 0.0))
        channels = int(report.get("output_channels", 1))
        peak = float(report.get("preview_peak", 0.0))
        commands = int(report.get("actual_playsound_command_count", 0))
        self.result_summary_var.set(
            f"{_format_duration(duration)} · {channels}ch · "
            f"峰{peak:.3f} · {commands:,}条"
        )
        video = report.get("video")
        if isinstance(video, Mapping):
            width = video.get("width", "?")
            height = video.get("height", "?")
            fps = video.get("fps", "?")
            quality = video.get("quality")
            quality_label = self._video_quality_label(quality) if isinstance(quality, str) else ""
            self.result_video_var.set(
                f"视频 {quality_label + ' · ' if quality_label else ''}{width}×{height} · {fps} fps · "
                + ("需启用视频资源包；" if video.get("requires_resource_pack") else "")
                + "在 Minecraft 中查看；本地试听仅播放音轨"
            )
            self.result_video_label.grid()
        else:
            self.result_video_var.set("")
            self.result_video_label.grid_remove()
        required = report.get("required_resource_pack")
        profile = required.get("device_profile") if isinstance(required, Mapping) else None
        if isinstance(profile, str) and profile in DEFAULT_DEVICE_PACK_PROFILES:
            status = inspect_device_pack(
                Path(self.bank_output_var.get()).expanduser(),
                profile,
            )
            state_text = PACK_STATE_TEXT[status.state]
            self.result_pack_var.set(f"{profile}·{state_text}")
            if status.state == "valid":
                self.pack_jump_button.grid_remove()
            else:
                self.pack_jump_button.grid()
        else:
            self.result_pack_var.set(
                ("无声视频 · 需视频资源包" if video.get("requires_resource_pack") else "无声视频 · 无需资源包")
                if isinstance(video, Mapping) and report.get("audio_enabled") is False
                else "所需资源包参数见分析报告"
            )
            self.pack_jump_button.grid_remove()
        self.convert_action_row.grid_remove()
        self.result_frame.grid()
        self._append_log("转换完成：" + ", ".join(str(path) for path in typed_outputs.values()))

    def _primary_bank_action(self) -> None:
        if self.busy:
            self._request_cancel()
        else:
            self._start_bank_build()

    def _start_bank_build(self) -> None:
        self.bank_error_var.set("")
        profiles = tuple(
            name
            for name in DEFAULT_DEVICE_PACK_PROFILES
            if self.profile_vars[name].get()
        )
        if not profiles:
            self.bank_error_var.set("请至少选择一个档位。")
            return
        output_dir = Path(
            self.bank_output_var.get() or "output/device_banks"
        ).expanduser()
        existing = [
            output_dir / f"wav2mc_{profile}_sine_bank.zip"
            for profile in profiles
            if (output_dir / f"wav2mc_{profile}_sine_bank.zip").exists()
        ]
        if existing and not messagebox.askyesno(
            "覆盖资源包",
            f"已有 {len(existing)} 个同名资源包，是否覆盖？",
            parent=self.root,
        ):
            return

        def work(
            progress_callback: ProgressCallback,
            cancel_check: Callable[[], bool],
        ) -> Mapping[str, Path]:
            return build_device_pack_set(
                output_dir=output_dir,
                base_config=AudioConfig(),
                pack_format=DEFAULT_RESOURCE_PACK_FORMAT,
                profile_names=profiles,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )

        self._persist_settings()
        self._run_task(
            "正在生成资源包",
            work,
            self._bank_build_finished,
            self.bank_button,
            "生成选中",
        )

    def _bank_build_finished(self, result: object) -> None:
        outputs = result
        if not isinstance(outputs, Mapping):
            raise TypeError("Resource pack task returned invalid outputs")
        self._refresh_pack_statuses()
        self._append_log("资源包生成完成：" + ", ".join(str(path) for path in outputs.values()))
        if self.last_report is not None:
            self._refresh_result_pack_status()

    def _run_task(
        self,
        status: str,
        work: Callable[[ProgressCallback, Callable[[], bool]], _T],
        on_success: Callable[[object], None],
        active_button: ttk.Button,
        idle_text: str,
    ) -> None:
        if self.busy:
            return
        self.busy = True
        self.cancel_event.clear()
        self.progress_var.set(0.0)
        self.status_var.set(status)
        self._append_log(status)
        self.task_success_callback = on_success
        self.active_button = active_button
        self.active_button_idle_text = idle_text
        self.convert_button.state(["disabled"])
        self.bank_button.state(["disabled"])
        active_button.state(["!disabled"])
        active_button.configure(text="取消", command=self._request_cancel)

        def report_progress(update: ProgressUpdate) -> None:
            self.event_queue.put(("progress", update))

        def runner() -> None:
            try:
                result = work(report_progress, self.cancel_event.is_set)
            except TaskCancelled:
                self.event_queue.put(("task_cancelled",))
            except Exception as exc:
                self.event_queue.put(("task_failed", exc))
            else:
                self.event_queue.put(("task_succeeded", result))

        threading.Thread(
            target=runner,
            name="wav2mc-gui-worker",
            daemon=True,
        ).start()

    def _request_cancel(self) -> None:
        if not self.busy or self.cancel_event.is_set():
            return
        self.cancel_event.set()
        self.status_var.set("正在安全取消…")
        if self.active_button is not None:
            self.active_button.configure(text="正在取消…")
            self.active_button.state(["disabled"])

    def _handle_progress(self, update: ProgressUpdate) -> None:
        percent = 100.0 * update.fraction
        if percent + 1e-9 < self.progress_var.get():
            return
        self.progress_var.set(percent)
        stage = STAGE_TEXT.get(update.stage, update.stage)
        self.status_var.set(f"{stage} · {percent:.0f}%")

    def _poll_events(self) -> None:
        if self.destroyed:
            return
        while True:
            try:
                event = self.event_queue.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "progress":
                self._handle_progress(event[1])  # type: ignore[arg-type]
            elif kind == "probe_success":
                self._probe_succeeded(
                    event[1], event[2], event[3]  # type: ignore[arg-type]
                )
            elif kind == "video_probe_success":
                self._video_probe_succeeded(*event[1:])  # type: ignore[arg-type]
            elif kind == "probe_error":
                self._probe_failed(
                    event[1], event[2], event[3]  # type: ignore[arg-type]
                )
            elif kind == "task_succeeded":
                self._task_succeeded(event[1])
            elif kind == "task_cancelled":
                self._task_cancelled()
            elif kind == "task_failed":
                self._task_failed(event[1])  # type: ignore[arg-type]
        if not self.destroyed:
            self.root.after(50, self._poll_events)

    def _restore_idle_controls(self, status: str) -> None:
        self.busy = False
        self.status_var.set(status)
        if self.active_button is not None:
            self.active_button.configure(
                text=self.active_button_idle_text,
                command=(
                    self._primary_conversion_action
                    if self.active_button is self.convert_button
                    else self._primary_bank_action
                ),
            )
        self.convert_button.state(["!disabled"])
        self.bank_button.state(["!disabled"])
        self.active_button = None

    def _task_succeeded(self, result: object) -> None:
        callback = self.task_success_callback
        self.progress_var.set(100.0)
        self._restore_idle_controls("完成")
        if self.close_pending:
            self._destroy()
            return
        try:
            if callback is not None:
                callback(result)
        except Exception as exc:
            self._task_failed(exc)

    def _task_cancelled(self) -> None:
        self._restore_idle_controls("已取消")
        self._append_log("任务已取消，正式输出未更改。")
        if self.close_pending:
            self._destroy()

    def _task_failed(self, error: Exception) -> None:
        if self.busy:
            self._restore_idle_controls("失败")
        else:
            self.status_var.set("失败")
        self._append_log(f"错误：{error}")
        self._set_details(True)
        if self.close_pending:
            self._destroy()

    def _toggle_details(self) -> None:
        self._set_details(not self.details_open)

    def _set_details(self, opened: bool) -> None:
        self.details_open = opened
        if opened:
            self.log.grid()
        else:
            self.log.grid_remove()

    def _append_log(self, message: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, message.rstrip() + "\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _bank_path_changed(self, *_args: object) -> None:
        self.root.after_idle(self._refresh_pack_statuses)

    def _refresh_pack_statuses(self) -> None:
        output_dir = Path(
            self.bank_output_var.get() or "output/device_banks"
        ).expanduser()
        for name in DEFAULT_DEVICE_PACK_PROFILES:
            try:
                state = inspect_device_pack(output_dir, name).state
            except (OSError, ValueError):
                state = "mismatch"
            self.pack_status_vars[name].set(PACK_STATE_TEXT[state])

    def _refresh_result_pack_status(self) -> None:
        if self.last_report is None:
            return
        required = self.last_report.get("required_resource_pack")
        if not isinstance(required, Mapping):
            return
        profile = required.get("device_profile")
        if not isinstance(profile, str) or profile not in DEFAULT_DEVICE_PACK_PROFILES:
            return
        status = inspect_device_pack(
            Path(self.bank_output_var.get()).expanduser(),
            profile,
        )
        self.result_pack_var.set(
            f"{profile}·{PACK_STATE_TEXT[status.state]}"
        )
        if status.state == "valid":
            self.pack_jump_button.grid_remove()
        else:
            self.pack_jump_button.grid()

    def _dismiss_result(self) -> None:
        self._stop_preview()
        self.result_frame.grid_remove()
        self.convert_action_row.grid()

    def _jump_to_required_pack(self) -> None:
        if self.last_report is not None:
            required = self.last_report.get("required_resource_pack")
            profile = required.get("device_profile") if isinstance(required, Mapping) else None
            if isinstance(profile, str) and profile in self.profile_vars:
                for name, variable in self.profile_vars.items():
                    variable.set(name == profile)
        self.notebook.select(self.bank_tab)

    def _play_preview(self) -> None:
        if self.last_outputs is None or "preview" not in self.last_outputs:
            return
        if not self.preview_player.available:
            self._open_external_preview()
            self.status_var.set("未找到 ffplay，已用系统播放器试听；暂停和跳转请在播放器中操作。")
            return
        try:
            path = Path(self.last_outputs["preview"]).expanduser().resolve()
            if self.preview_player.path != path:
                self.preview_player.load(path)
            if self.preview_player.state == "playing":
                self.preview_player.pause()
            else:
                self.preview_player.play()
        except (OSError, ValueError, RuntimeError) as exc:
            self._append_log(f"无法试听：{exc}")
            self.status_var.set("试听失败，可尝试系统播放器。")
            self._set_details(True)
        self._poll_preview()

    def _open_external_preview(self) -> None:
        self._stop_preview()
        if self.last_outputs is not None and "preview" in self.last_outputs:
            self._open_path(Path(self.last_outputs["preview"]), False)

    def _stop_preview(self) -> None:
        if self.preview_after is not None:
            self.root.after_cancel(self.preview_after)
            self.preview_after = None
        self.preview_player.stop()
        self.preview_dragging = False
        self._update_preview_controls()

    def _update_preview_controls(self) -> None:
        player = self.preview_player
        self.preview_play_button.configure(
            text="暂停" if player.state == "playing" else (
                "继续" if player.state == "paused" else "试听"
            ),
        )
        enabled = player.available and player.path is not None
        state = tk.NORMAL if enabled else tk.DISABLED
        self.preview_stop_button.configure(state=state)
        self.preview_seek.configure(to=max(0.001, player.duration), state=state)
        if not self.preview_dragging:
            self.preview_position_var.set(player.position)
            self.preview_time_var.set(
                f"{_format_duration(player.position)} / {_format_duration(player.duration)}"
            )

    def _poll_preview(self) -> None:
        if self.preview_after is not None:
            self.root.after_cancel(self.preview_after)
            self.preview_after = None
        if self.destroyed:
            return
        self.preview_player.poll()
        error = self.preview_player.error
        if error and error != self.preview_error:
            self.preview_error = error
            self._append_log(error)
            self.status_var.set("试听失败，请检查音频设备或使用系统播放器。")
            self._set_details(True)
        elif not error:
            self.preview_error = None
        self._update_preview_controls()
        if self.preview_player.state == "playing":
            self.preview_after = self.root.after(100, self._poll_preview)

    def _begin_preview_seek(self, _event: tk.Event[tk.Misc]) -> None:
        self.preview_dragging = True

    def _preview_seek_changed(self, _value: str) -> None:
        if self.preview_dragging:
            self.preview_time_var.set(
                f"{_format_duration(self.preview_position_var.get())} / "
                f"{_format_duration(self.preview_player.duration)}"
            )

    def _commit_preview_seek(self, event: tk.Event[tk.Misc]) -> None:
        if event.type == tk.EventType.KeyRelease and event.keysym not in {
            "Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next",
        }:
            return
        self.preview_dragging = False
        if not self.preview_player.available or self.preview_player.path is None:
            return
        try:
            self.preview_player.seek(self.preview_position_var.get())
        except (OSError, ValueError, RuntimeError) as exc:
            self._append_log(f"无法跳转试听位置：{exc}")
            self.status_var.set("试听跳转失败，可尝试系统播放器。")
        self._poll_preview()

    def _open_result_directory(self) -> None:
        if self.last_outputs:
            self._open_path(Path(next(iter(self.last_outputs.values()))).parent, True)

    def _refresh_command_menu(self) -> None:
        self.command_menu.delete(0, tk.END)
        commands = result_function_commands(self.last_report or {})
        for action in commands:
            self.command_menu.add_command(
                label=GAME_COMMAND_LABELS[action],
                command=lambda selected=action: self._copy_result_command(selected),
            )

    def _copy_result_command(self, action: str = "start") -> None:
        if self.last_report is None:
            return
        try:
            command = result_function_command(self.last_report, action)
        except ValueError as exc:
            self.status_var.set(str(exc))
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(command)
        self.root.update_idletasks()
        self.status_var.set(f"已复制 {command}")

    def _open_path(self, path: Path, create_directory: bool) -> None:
        try:
            path = path.expanduser().resolve()
            if create_directory:
                path.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(
                    ["open", str(path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                subprocess.Popen(
                    ["xdg-open", str(path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except OSError as exc:
            self._append_log(f"无法打开 {path}：{exc}")
            self._set_details(True)

    def _current_settings(self) -> GuiSettings:
        input_path = Path(self.input_var.get()).expanduser() if self.input_var.get() else None
        try:
            gain = float(self.gain_db_var.get())
        except (tk.TclError, ValueError):
            gain = 0.0
        try:
            native = self.video_quality_var.get() == "native"
            pixel = self.video_pixel_size_var.get().strip()
            video = VideoConfig(
                width=None if native else int(self.video_width_var.get()),
                height=None if native else int(self.video_height_var.get()),
                fps=int(self.video_fps_var.get()),
                pixel_size=None if native and pixel in ("", "自动", "None") else float(pixel),
                renderer=self.video_renderer_var.get(), native_resolution=native,
            )
            video.validate()
        except (tk.TclError, TypeError, ValueError):
            video = video_config_for_quality()
        try:
            video_stream = max(0, int(self.video_stream_var.get()))
        except (tk.TclError, ValueError):
            video_stream = 0
        return GuiSettings(
            last_input_dir=str(input_path.parent) if input_path else self.settings.last_input_dir,
            output_dir=self.output_var.get() or "output",
            bank_output_dir=self.bank_output_var.get() or "output/device_banks",
            mode=(
                self.mode_var.get()
                if self.mode_var.get() in DEFAULT_DEVICE_PACK_PROFILES
                else "normal"
            ),
            gain_db=min(MAX_GUI_GAIN_DB, max(MIN_GUI_GAIN_DB, gain)),
            preserve_stereo=self.stereo_var.get(),
            psychoacoustic_masking=self.masking_var.get(),
            advanced_open=self.advanced_open,
            selected_profiles=tuple(
                name for name in DEFAULT_DEVICE_PACK_PROFILES if self.profile_vars[name].get()
            ),
            video_enabled=self.video_enabled_var.get(),
            video_width=video.width,
            video_height=video.height,
            video_fps=video.fps,
            video_pixel_size=video.pixel_size,
            video_stream=video_stream,
            include_audio=self.include_audio_var.get(),
            video_quality="native" if video.native_resolution else self.video_quality_var.get(),
            video_renderer=video.renderer,
        )

    def _persist_settings(self) -> None:
        try:
            self.settings = self._current_settings()
            save_gui_settings(self.settings, self.settings_path)
        except OSError as exc:
            self._append_log(f"无法保存界面设置：{exc}")

    def _on_close(self) -> None:
        if self.busy:
            if not messagebox.askyesno(
                "取消任务并关闭",
                "当前任务仍在运行。是否安全取消并在工作线程退出后关闭？",
                parent=self.root,
            ):
                return
            self.close_pending = True
            self._request_cancel()
            return
        self._destroy()

    def _destroy(self) -> None:
        if self.destroyed:
            return
        self._stop_preview()
        self.preview_player.close()
        self._persist_settings()
        self.destroyed = True
        self.root.destroy()


def launch_gui(
    initial_input: Path | None = None,
    output_dir: Path | None = None,
) -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise RuntimeError(f"无法启动图形界面：{exc}") from exc
    Wav2McApp(root, initial_input=initial_input, initial_output_dir=output_dir)
    root.mainloop()


def main() -> int:
    try:
        launch_gui()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
