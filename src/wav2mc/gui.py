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
    AudioConfig,
    LoudnessCalibration,
    device_audio_config,
)
from .gui_state import (
    GuiSettings,
    inspect_device_pack,
    load_gui_settings,
    save_gui_settings,
)
from .pipeline import convert_audio
from .utils import ProgressCallback, ProgressUpdate, TaskCancelled, safe_namespace


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
    "analyse": "分析",
    "reconstruct": "重建预览",
    "datapack": "生成数据包",
    "report": "写入报告",
    "resource_pack": "生成资源包",
    "compress": "压缩",
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


def conversion_output_paths(output_dir: Path, song_name: str) -> dict[str, Path]:
    namespace = safe_namespace(song_name)
    return {
        "data_pack": output_dir / f"{namespace}_datapack.zip",
        "preview": output_dir / f"{namespace}_preview.wav",
        "report": output_dir / f"{namespace}_analysis.json",
    }


def result_function_command(report: Mapping[str, object]) -> str:
    namespace = safe_namespace(str(report.get("song_namespace") or "song"))
    return f"/function {namespace}:start"


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
        self.last_outputs: Mapping[str, Path] | None = None
        self.last_report: Mapping[str, object] | None = None
        self.task_success_callback: Callable[[object], None] | None = None
        self.active_button: ttk.Button | None = None
        self.active_button_idle_text = ""
        self.details_open = False

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

        self._configure_window()
        self._configure_styles()
        self._build_layout()
        self.mode_var.trace_add("write", self._update_mode_summary)
        self.stereo_var.trace_add("write", self._update_mode_summary)
        self.gain_db_var.trace_add("write", self._update_gain_text)
        self.bank_output_var.trace_add("write", self._bank_path_changed)
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
        self.root.title("wav2mc - Minecraft 音频转换器")
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
        ttk.Label(left, text="音频文件", style="Surface.TLabel").grid(
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
        preview_button = ttk.Button(
            self.result_frame,
            text="试听",
            width=4,
            style="Result.TButton",
            command=self._play_preview,
        )
        preview_button.grid(row=0, column=3, padx=(2, 0))
        _ToolTip(preview_button, lambda: "试听预览")
        directory_button = ttk.Button(
            self.result_frame,
            text="目录",
            width=4,
            style="Result.TButton",
            command=self._open_result_directory,
        )
        directory_button.grid(row=0, column=4, padx=(2, 0))
        _ToolTip(directory_button, lambda: "打开输出目录")
        copy_button = ttk.Button(
            self.result_frame,
            text="复制",
            width=4,
            style="Result.TButton",
            command=self._copy_result_command,
        )
        copy_button.grid(row=0, column=5, padx=(2, 0))
        _ToolTip(copy_button, lambda: "复制 /function <namespace>:start")
        ttk.Button(
            self.result_frame,
            text="×",
            width=2,
            style="Result.TButton",
            command=self._dismiss_result,
        ).grid(row=0, column=6, sticky="e", padx=(2, 0))
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
            title="选择音频或媒体文件",
            initialdir=str(initial_dir),
            filetypes=(
                (
                    "音频与媒体",
                    "*.wav *.mp3 *.flac *.m4a *.m4s *.aac *.ogg "
                    "*.opus *.aiff *.mp4 *.mkv *.webm",
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
        self.media_info_var.set("正在探测…")
        self.audio_stream_var.set(0)
        self.stream_display_var.set("0 · 默认音轨")
        self.stream_box.configure(values=("0 · 默认音轨",))

        def runner() -> None:
            try:
                info = probe_media(path)
            except Exception as exc:
                self.event_queue.put(("probe_error", generation, path, exc))
            else:
                self.event_queue.put(("probe_success", generation, path, info))

        threading.Thread(
            target=runner,
            name="wav2mc-media-probe",
            daemon=True,
        ).start()

    def _probe_succeeded(self, generation: int, path: Path, info: MediaInfo) -> None:
        if generation != self.probe_generation or self.input_var.get() != str(path):
            return
        self.media_info = info
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
        self.stream_box.configure(values=values)
        self.stream_display_var.set(values[0])
        self.audio_stream_var.set(0)

    def _probe_failed(self, generation: int, path: Path, error: Exception) -> None:
        if generation != self.probe_generation or self.input_var.get() != str(path):
            return
        self.media_info_var.set("探测失败 · 使用音轨 0")
        self._append_log(f"媒体探测失败：{error}")

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
        except (tk.TclError, ValueError) as exc:
            self.convert_error_var.set(f"参数无效：{exc}")
            return
        if not MIN_GUI_GAIN_DB <= gain_db <= MAX_GUI_GAIN_DB:
            self.convert_error_var.set("转换增益必须在 -24 到 +12 dB 之间。")
            return
        if stream < 0:
            self.convert_error_var.set("音轨索引不能为负数。")
            return

        targets = conversion_output_paths(output_dir, song_name)
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
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )

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
        self.last_outputs = typed_outputs
        self.last_report = report
        duration = float(report.get("input_duration_seconds", 0.0))
        channels = int(report.get("output_channels", 1))
        peak = float(report.get("preview_peak", 0.0))
        commands = int(report.get("actual_playsound_command_count", 0))
        self.result_summary_var.set(
            f"{_format_duration(duration)} · {channels}ch · "
            f"峰{peak:.3f} · {commands:,}条"
        )
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
            self.result_pack_var.set("所需资源包参数见分析报告")
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
        if self.last_outputs is not None and "preview" in self.last_outputs:
            self._open_path(Path(self.last_outputs["preview"]), False)

    def _open_result_directory(self) -> None:
        if self.last_outputs:
            self._open_path(Path(next(iter(self.last_outputs.values()))).parent, True)

    def _copy_result_command(self) -> None:
        if self.last_report is None:
            return
        command = result_function_command(self.last_report)
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
