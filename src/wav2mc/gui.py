from __future__ import annotations

import math
import os
import subprocess
import sys
import threading
import tkinter as tk
from collections.abc import Callable, Mapping
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import TypeVar

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
from .pipeline import convert_audio
from .utils import safe_namespace


_T = TypeVar("_T")
MIN_GUI_GAIN_DB = -24.0
MAX_GUI_GAIN_DB = 12.0


class _ScrollableFrame(ttk.Frame):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(
            self,
            background="#f3f5f4",
            highlightthickness=0,
            borderwidth=0,
        )
        scrollbar = ttk.Scrollbar(
            self,
            orient=tk.VERTICAL,
            command=self.canvas.yview,
        )
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.body = ttk.Frame(self.canvas)
        self._body_window = self.canvas.create_window(
            (0, 0),
            window=self.body,
            anchor=tk.NW,
        )
        self.body.bind("<Configure>", self._update_scroll_region)
        self.canvas.bind("<Configure>", self._resize_body)
        self.canvas.bind("<Enter>", self._bind_mousewheel)
        self.canvas.bind("<Leave>", self._unbind_mousewheel)

    def _update_scroll_region(self, _event: tk.Event[tk.Misc]) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize_body(self, event: tk.Event[tk.Misc]) -> None:
        self.canvas.itemconfigure(self._body_window, width=event.width)

    def _bind_mousewheel(self, _event: tk.Event[tk.Misc]) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind_all("<Button-4>", self._on_mousewheel)
        self.canvas.bind_all("<Button-5>", self._on_mousewheel)

    def _unbind_mousewheel(self, _event: tk.Event[tk.Misc]) -> None:
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")

    def _on_mousewheel(self, event: tk.Event[tk.Misc]) -> None:
        if event.num == 4:
            steps = -1
        elif event.num == 5:
            steps = 1
        else:
            steps = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(steps, "units")


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


def conversion_output_paths(
    output_dir: Path,
    song_name: str,
) -> dict[str, Path]:
    namespace = safe_namespace(song_name)
    return {
        "data_pack": output_dir / f"{namespace}_datapack.zip",
        "preview": output_dir / f"{namespace}_preview.wav",
        "report": output_dir / f"{namespace}_analysis.json",
    }


class Wav2McApp:
    def __init__(
        self,
        root: tk.Tk,
        initial_input: Path | None = None,
        initial_output_dir: Path = Path("output"),
    ) -> None:
        self.root = root
        self.busy = False
        self.last_output_dir = initial_output_dir
        self.action_buttons: list[ttk.Button] = []

        self.input_var = tk.StringVar(
            value=str(initial_input) if initial_input else ""
        )
        self.output_var = tk.StringVar(value=str(initial_output_dir))
        self.song_name_var = tk.StringVar(
            value=initial_input.stem if initial_input else ""
        )
        self.audio_stream_var = tk.IntVar(value=0)
        self.mode_var = tk.StringVar(value="normal")
        self.gain_db_var = tk.StringVar(value="0.0")
        self.gain_text_var = tk.StringVar(value="1.00x")
        self.masking_var = tk.BooleanVar(value=True)
        self.bank_output_var = tk.StringVar(value="output/device_banks")
        self.mode_summary_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪")

        self._configure_window()
        self._configure_styles()
        self._build_layout()
        self.mode_var.trace_add("write", self._update_mode_summary)
        self.gain_db_var.trace_add("write", self._update_gain_text)
        self._update_mode_summary()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_window(self) -> None:
        self.root.title("wav2mc - Minecraft 音频转换器")
        self.root.geometry("980x960")
        self.root.minsize(800, 700)
        self.root.configure(background="#f3f5f4")

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        font = ("Noto Sans CJK SC", 10)
        style.configure(".", font=font, foreground="#202a25")
        style.configure("TFrame", background="#f3f5f4")
        style.configure(
            "Header.TLabel",
            background="#f3f5f4",
            font=(font[0], 20, "bold"),
        )
        style.configure("Muted.TLabel", background="#f3f5f4", foreground="#64716a")
        style.configure(
            "Version.TLabel",
            background="#e4ebe7",
            foreground="#315445",
            padding=(10, 5),
        )
        style.configure(
            "Panel.TLabelframe",
            background="#ffffff",
            bordercolor="#d6ddd9",
            relief="solid",
            padding=14,
        )
        style.configure(
            "Panel.TLabelframe.Label",
            background="#ffffff",
            foreground="#344039",
            font=(font[0], 10, "bold"),
        )
        style.configure("Panel.TFrame", background="#ffffff")
        style.configure("Panel.TLabel", background="#ffffff")
        style.configure(
            "PanelMuted.TLabel",
            background="#ffffff",
            foreground="#64716a",
        )
        style.configure("TEntry", padding=6, fieldbackground="#ffffff")
        style.configure("TCombobox", padding=5, fieldbackground="#ffffff")
        style.configure("TSpinbox", padding=5, fieldbackground="#ffffff")
        style.configure("TButton", padding=(12, 7))
        style.configure(
            "Accent.TButton",
            background="#356b55",
            foreground="#ffffff",
            bordercolor="#2c5c48",
            padding=(16, 8),
        )
        style.map(
            "Accent.TButton",
            background=[("active", "#2d5d49"), ("disabled", "#9aaba3")],
            foreground=[("disabled", "#edf1ef")],
        )
        style.configure("TNotebook", background="#f3f5f4", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 9))
        style.map(
            "TNotebook.Tab",
            background=[("selected", "#ffffff"), ("!selected", "#e6ebe8")],
            foreground=[("selected", "#244c3c"), ("!selected", "#58645e")],
        )
        style.configure(
            "Horizontal.TProgressbar",
            background="#3f7c63",
            troughcolor="#dfe5e2",
        )

    def _build_layout(self) -> None:
        shell = ttk.Frame(self.root, padding=(24, 18, 24, 16))
        shell.grid(row=0, column=0, sticky="nsew")
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(1, weight=1)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        header = ttk.Frame(shell)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="wav2mc", style="Header.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
        )
        ttk.Label(
            header,
            text=f"Minecraft Java {DEFAULT_MINECRAFT_VERSION}",
            style="Version.TLabel",
        ).grid(row=0, column=1, sticky="e")
        notebook = ttk.Notebook(shell)
        notebook.grid(row=1, column=0, sticky="nsew")
        convert_tab = ttk.Frame(notebook, padding=(0, 14, 0, 0))
        bank_tab = ttk.Frame(notebook, padding=(0, 14, 0, 0))
        notebook.add(convert_tab, text="音频转换")
        notebook.add(bank_tab, text="资源包")
        self._build_convert_tab(convert_tab)
        self._build_bank_tab(bank_tab)

        activity = ttk.Frame(shell)
        activity.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        activity.columnconfigure(0, weight=1)
        ttk.Label(activity, textvariable=self.status_var, style="Muted.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
        )
        self.progress = ttk.Progressbar(activity, mode="indeterminate", length=180)
        self.progress.grid(row=0, column=1, sticky="e")

        self.log = ScrolledText(
            activity,
            height=3,
            wrap=tk.WORD,
            font=("Noto Sans Mono CJK SC", 9),
            background="#ffffff",
            foreground="#26312b",
            insertbackground="#26312b",
            selectbackground="#b7d1c4",
            borderwidth=1,
            relief=tk.SOLID,
            padx=10,
            pady=8,
            state=tk.DISABLED,
        )
        self.log.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self._append_log("请选择音频文件，或先生成与模式匹配的资源包。")

    def _build_convert_tab(self, tab: ttk.Frame) -> None:
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        scroller = _ScrollableFrame(tab)
        scroller.grid(row=0, column=0, sticky="nsew")
        body = scroller.body
        body.columnconfigure(0, weight=1)

        paths = ttk.LabelFrame(body, text="输入与输出", style="Panel.TLabelframe")
        paths.grid(row=0, column=0, sticky="ew")
        paths.columnconfigure(1, weight=1)

        ttk.Label(paths, text="音频文件", style="Panel.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 12), pady=5
        )
        ttk.Entry(paths, textvariable=self.input_var).grid(
            row=0, column=1, sticky="ew", pady=5
        )
        ttk.Button(paths, text="选择…", command=self._choose_input).grid(
            row=0, column=2, padx=(8, 0), pady=5
        )

        ttk.Label(paths, text="输出目录", style="Panel.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 12), pady=5
        )
        ttk.Entry(paths, textvariable=self.output_var).grid(
            row=1, column=1, sticky="ew", pady=5
        )
        ttk.Button(
            paths,
            text="选择…",
            command=lambda: self._choose_directory(self.output_var),
        ).grid(row=1, column=2, padx=(8, 0), pady=5)

        ttk.Label(paths, text="歌曲名称", style="Panel.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 12), pady=5
        )
        ttk.Entry(paths, textvariable=self.song_name_var).grid(
            row=2, column=1, sticky="ew", pady=5
        )
        stream_frame = ttk.Frame(paths, style="Panel.TFrame")
        stream_frame.grid(row=2, column=2, sticky="e", padx=(8, 0), pady=5)
        ttk.Label(stream_frame, text="音轨", style="Panel.TLabel").grid(
            row=0, column=0, padx=(0, 5)
        )
        ttk.Spinbox(
            stream_frame,
            from_=0,
            to=15,
            width=4,
            textvariable=self.audio_stream_var,
        ).grid(row=0, column=1)

        options = ttk.LabelFrame(body, text="转换配置", style="Panel.TLabelframe")
        options.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        options.columnconfigure(1, weight=1)

        ttk.Label(options, text="质量模式", style="Panel.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 12), pady=5
        )
        mode_box = ttk.Combobox(
            options,
            textvariable=self.mode_var,
            values=DEFAULT_DEVICE_PACK_PROFILES,
            state="readonly",
            width=18,
        )
        mode_box.grid(row=0, column=1, sticky="w", pady=5)

        ttk.Label(options, text="转换增益", style="Panel.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 12), pady=5
        )
        gain_row = ttk.Frame(options, style="Panel.TFrame")
        gain_row.grid(row=1, column=1, sticky="ew", pady=5)
        ttk.Spinbox(
            gain_row,
            from_=MIN_GUI_GAIN_DB,
            to=MAX_GUI_GAIN_DB,
            increment=0.5,
            format="%.1f",
            width=8,
            justify=tk.RIGHT,
            textvariable=self.gain_db_var,
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            gain_row,
            text="dB",
            style="Panel.TLabel",
        ).grid(row=0, column=1, padx=(6, 14))
        ttk.Label(
            gain_row,
            textvariable=self.gain_text_var,
            style="PanelMuted.TLabel",
            width=9,
        ).grid(row=0, column=2)
        ttk.Button(
            gain_row,
            text="归零",
            command=lambda: self.gain_db_var.set("0.0"),
        ).grid(row=0, column=3, padx=(10, 0))

        ttk.Label(options, text="心理声学", style="Panel.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 12), pady=5
        )
        ttk.Checkbutton(
            options,
            text="启用 A-weighting 与 Bark 掩蔽",
            variable=self.masking_var,
        ).grid(row=2, column=1, sticky="w", pady=5)

        ttk.Separator(options).grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(10, 8)
        )
        ttk.Label(
            options,
            textvariable=self.mode_summary_var,
            style="PanelMuted.TLabel",
            wraplength=650,
            justify=tk.LEFT,
        ).grid(row=4, column=0, columnspan=2, sticky="w")

        actions = ttk.Frame(tab)
        actions.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        actions.columnconfigure(0, weight=1)
        open_button = ttk.Button(
            actions,
            text="打开输出目录",
            command=lambda: self._open_directory(
                Path(self.output_var.get() or "output")
            ),
        )
        open_button.grid(row=0, column=1, padx=(0, 8))
        convert_button = ttk.Button(
            actions,
            text="开始转换",
            style="Accent.TButton",
            command=self._start_conversion,
        )
        convert_button.grid(row=0, column=2)
        self.action_buttons.append(convert_button)

    def _build_bank_tab(self, tab: ttk.Frame) -> None:
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        scroller = _ScrollableFrame(tab)
        scroller.grid(row=0, column=0, sticky="nsew")
        body = scroller.body
        body.columnconfigure(0, weight=1)

        settings = ttk.LabelFrame(body, text="资源包配置", style="Panel.TLabelframe")
        settings.grid(row=0, column=0, sticky="ew")
        settings.columnconfigure(1, weight=1)

        ttk.Label(settings, text="输出目录", style="Panel.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 12), pady=5
        )
        ttk.Entry(settings, textvariable=self.bank_output_var).grid(
            row=0, column=1, sticky="ew", pady=5
        )
        ttk.Button(
            settings,
            text="选择…",
            command=lambda: self._choose_directory(self.bank_output_var),
        ).grid(row=0, column=2, padx=(8, 0), pady=5)

        ttk.Label(settings, text="当前模式", style="Panel.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 12), pady=5
        )
        ttk.Combobox(
            settings,
            textvariable=self.mode_var,
            values=DEFAULT_DEVICE_PACK_PROFILES,
            state="readonly",
            width=18,
        ).grid(row=1, column=1, sticky="w", pady=5)
        ttk.Label(
            settings,
            text=f"资源包格式 {DEFAULT_RESOURCE_PACK_FORMAT}",
            style="PanelMuted.TLabel",
        ).grid(row=1, column=2, sticky="e", padx=(8, 0), pady=5)

        ttk.Separator(settings).grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=(10, 8)
        )
        ttk.Label(
            settings,
            textvariable=self.mode_summary_var,
            style="PanelMuted.TLabel",
            wraplength=650,
            justify=tk.LEFT,
        ).grid(row=3, column=0, columnspan=3, sticky="w")

        actions = ttk.Frame(tab)
        actions.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        actions.columnconfigure(0, weight=1)
        ttk.Button(
            actions,
            text="打开资源包目录",
            command=lambda: self._open_directory(
                Path(self.bank_output_var.get() or "output/device_banks")
            ),
        ).grid(row=0, column=1, padx=(0, 8))
        current_button = ttk.Button(
            actions,
            text="生成当前模式",
            command=lambda: self._start_bank_build(all_profiles=False),
        )
        current_button.grid(row=0, column=2, padx=(0, 8))
        all_button = ttk.Button(
            actions,
            text="生成全部模式",
            style="Accent.TButton",
            command=lambda: self._start_bank_build(all_profiles=True),
        )
        all_button.grid(row=0, column=3)
        self.action_buttons.extend((current_button, all_button))

    def _choose_input(self) -> None:
        current = (
            Path(self.input_var.get()).expanduser()
            if self.input_var.get()
            else None
        )
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="选择音频或媒体文件",
            initialdir=str(current.parent if current else Path.cwd()),
            filetypes=(
                (
                    "音频与媒体",
                    "*.wav *.mp3 *.flac *.m4a *.m4s *.aac *.ogg "
                    "*.opus *.aiff *.mp4 *.mkv *.webm",
                ),
                ("所有文件", "*"),
            ),
        )
        if selected:
            previous_stem = current.stem if current else ""
            self.input_var.set(selected)
            if (
                not self.song_name_var.get().strip()
                or self.song_name_var.get() == previous_stem
            ):
                self.song_name_var.set(Path(selected).stem)

    def _choose_directory(self, variable: tk.StringVar) -> None:
        initial = Path(variable.get()).expanduser() if variable.get() else Path.cwd()
        selected = filedialog.askdirectory(
            parent=self.root,
            title="选择输出目录",
            initialdir=str(initial),
        )
        if selected:
            variable.set(selected)

    def _update_mode_summary(self, *_args: object) -> None:
        try:
            summary = mode_summary(self.mode_var.get())
        except ValueError:
            summary = "请选择有效模式"
        self.mode_summary_var.set(summary)

    def _update_gain_text(self, *_args: object) -> None:
        try:
            multiplier = gain_multiplier_from_db(float(self.gain_db_var.get()))
        except ValueError:
            self.gain_text_var.set("无效")
        else:
            self.gain_text_var.set(f"{multiplier:.2f}x")

    def _start_conversion(self) -> None:
        source = Path(self.input_var.get()).expanduser()
        if not source.is_file():
            messagebox.showerror("无法转换", "请选择存在的音频或媒体文件。", parent=self.root)
            return

        output_dir = Path(self.output_var.get() or "output").expanduser()
        song_name = self.song_name_var.get().strip() or source.stem
        targets = conversion_output_paths(output_dir, song_name)
        existing = [path for path in targets.values() if path.exists()]
        if existing and not messagebox.askyesno(
            "覆盖输出",
            f"已有 {len(existing)} 个同名输出文件，是否覆盖？",
            parent=self.root,
        ):
            return

        mode = self.mode_var.get()
        try:
            stream = self.audio_stream_var.get()
            gain_db = float(self.gain_db_var.get())
            masking = self.masking_var.get()
            config = mode_audio_config(mode)
        except (tk.TclError, ValueError) as exc:
            messagebox.showerror("参数错误", str(exc), parent=self.root)
            return
        if not MIN_GUI_GAIN_DB <= gain_db <= MAX_GUI_GAIN_DB:
            messagebox.showerror(
                "参数错误",
                f"转换增益必须在 {MIN_GUI_GAIN_DB:.0f} 到 "
                f"+{MAX_GUI_GAIN_DB:.0f} dB 之间。",
                parent=self.root,
            )
            return
        if stream < 0:
            messagebox.showerror("参数错误", "音轨索引不能为负数。", parent=self.root)
            return

        profile = DEVICE_PROFILES[mode]
        quality = QUALITY_PROFILES[profile.quality_name]
        gain = gain_multiplier_from_db(gain_db)

        def work() -> Mapping[str, Path]:
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
            )

        self.last_output_dir = output_dir
        display_name = source.name
        if len(display_name) > 36:
            display_name = display_name[:33] + "..."
        self._run_task(
            f"正在转换 {display_name}（{mode}）…",
            work,
            self._conversion_finished,
        )

    def _conversion_finished(self, outputs: Mapping[str, Path]) -> None:
        self._append_log("转换完成：")
        for label, path in outputs.items():
            self._append_log(f"  {label}: {path.resolve()}")
        messagebox.showinfo(
            "转换完成",
            "数据包、预览 WAV 和分析报告已生成。",
            parent=self.root,
        )

    def _start_bank_build(self, all_profiles: bool) -> None:
        output_dir = Path(
            self.bank_output_var.get() or "output/device_banks"
        ).expanduser()
        profiles = (
            DEFAULT_DEVICE_PACK_PROFILES
            if all_profiles
            else (self.mode_var.get(),)
        )
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

        def work() -> Mapping[str, Path]:
            return build_device_pack_set(
                output_dir=output_dir,
                base_config=AudioConfig(),
                pack_format=DEFAULT_RESOURCE_PACK_FORMAT,
                profile_names=tuple(profiles),
            )

        self.last_output_dir = output_dir
        label = "全部推荐模式" if all_profiles else profiles[0]
        self._run_task(
            f"正在生成资源包（{label}）…",
            work,
            self._bank_build_finished,
        )

    def _bank_build_finished(self, outputs: Mapping[str, Path]) -> None:
        self._append_log("资源包生成完成：")
        for label, path in outputs.items():
            self._append_log(f"  {label}: {path.resolve()}")
        messagebox.showinfo(
            "资源包完成",
            "所选资源包和清单已生成。",
            parent=self.root,
        )

    def _run_task(
        self,
        status: str,
        work: Callable[[], _T],
        on_success: Callable[[_T], None],
    ) -> None:
        if self.busy:
            return
        self.busy = True
        self.status_var.set(status)
        self._append_log(status)
        self.progress.start(12)
        for button in self.action_buttons:
            button.state(["disabled"])

        def runner() -> None:
            try:
                result = work()
            except Exception as exc:
                self.root.after(0, lambda error=exc: self._task_failed(error))
            else:
                self.root.after(
                    0,
                    lambda value=result: self._task_succeeded(value, on_success),
                )

        threading.Thread(target=runner, name="wav2mc-gui-worker", daemon=True).start()

    def _task_succeeded(
        self,
        result: _T,
        on_success: Callable[[_T], None],
    ) -> None:
        self._set_idle("完成")
        on_success(result)

    def _task_failed(self, error: Exception) -> None:
        self._set_idle("失败")
        self._append_log(f"错误：{error}")
        messagebox.showerror(
            "任务失败",
            str(error)[:1200],
            parent=self.root,
        )

    def _set_idle(self, status: str) -> None:
        self.busy = False
        self.status_var.set(status)
        self.progress.stop()
        for button in self.action_buttons:
            button.state(["!disabled"])

    def _append_log(self, message: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, message.rstrip() + "\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _open_directory(self, path: Path) -> None:
        try:
            path = path.expanduser().resolve()
            path.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("无法打开目录", str(exc), parent=self.root)

    def _on_close(self) -> None:
        if self.busy:
            messagebox.showwarning(
                "任务正在运行",
                "请等待当前转换或资源包生成完成后再关闭。",
                parent=self.root,
            )
            return
        self.root.destroy()


def launch_gui(
    initial_input: Path | None = None,
    output_dir: Path = Path("output"),
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
