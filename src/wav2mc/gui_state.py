from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import (
    DEFAULT_DEVICE_PACK_PROFILES,
    DEFAULT_MINECRAFT_VERSION,
    DEFAULT_RESOURCE_PACK_FORMAT,
    DEFAULT_VIDEO_QUALITY,
    DEVICE_PROFILES,
    AudioConfig,
    VideoConfig,
    audio_config_metadata,
    device_audio_config,
    video_quality_name,
    video_config_for_quality,
)


SETTINGS_VERSION = 1


@dataclass(frozen=True)
class GuiSettings:
    version: int = SETTINGS_VERSION
    last_input_dir: str = ""
    output_dir: str = "output"
    bank_output_dir: str = "output/device_banks"
    mode: str = "normal"
    gain_db: float = 0.0
    preserve_stereo: bool = True
    psychoacoustic_masking: bool = True
    advanced_open: bool = False
    selected_profiles: tuple[str, ...] = ("normal",)
    video_enabled: bool = False
    video_width: int | None = None
    video_height: int | None = None
    video_fps: int | None = None
    video_pixel_size: float | None = None
    video_stream: int = 0
    include_audio: bool = True
    video_quality: str | None = None
    video_renderer: str = "auto"

    def __post_init__(self) -> None:
        native = self.video_quality == "native"
        default = video_config_for_quality("native" if native else DEFAULT_VIDEO_QUALITY)
        if self.video_fps is None:
            object.__setattr__(self, "video_fps", default.fps)
        if not native:
            for name, value in (
                ("video_width", default.width), ("video_height", default.height),
                ("video_pixel_size", default.pixel_size),
            ):
                if getattr(self, name) is None:
                    object.__setattr__(self, name, value)
        if self.video_quality == "custom":
            return
        try:
            video = VideoConfig(
                self.video_width, self.video_height, self.video_fps,
                self.video_pixel_size, renderer=self.video_renderer,
                native_resolution=native,
            )
            quality = video_quality_name(video)
        except (TypeError, ValueError):
            quality = DEFAULT_VIDEO_QUALITY
        object.__setattr__(self, "video_quality", quality)


@dataclass(frozen=True)
class PackStatus:
    state: str
    path: Path


def gui_settings_path() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "wav2mc" / "gui.json"


def _validated_settings(payload: object) -> GuiSettings:
    if not isinstance(payload, dict) or payload.get("version") != SETTINGS_VERSION:
        return GuiSettings()
    mode = str(payload.get("mode") or "normal")
    if mode not in DEFAULT_DEVICE_PACK_PROFILES:
        mode = "normal"
    try:
        gain_db = float(payload.get("gain_db", 0.0))
    except (TypeError, ValueError):
        gain_db = 0.0
    if not math.isfinite(gain_db):
        gain_db = 0.0
    gain_db = min(12.0, max(-24.0, gain_db))
    selected_payload = payload.get("selected_profiles", ())
    if not isinstance(selected_payload, (list, tuple)):
        selected_payload = ()
    selected = tuple(
        profile
        for profile in selected_payload
        if profile in DEFAULT_DEVICE_PACK_PROFILES
    )
    preserve_stereo = payload.get("preserve_stereo", True)
    if not isinstance(preserve_stereo, bool):
        preserve_stereo = True
    masking = payload.get("psychoacoustic_masking", True)
    if not isinstance(masking, bool):
        masking = True
    advanced_open = payload.get("advanced_open", False)
    if not isinstance(advanced_open, bool):
        advanced_open = False
    video_enabled = payload.get("video_enabled", False)
    if not isinstance(video_enabled, bool):
        video_enabled = False
    include_audio = payload.get("include_audio", True)
    if not isinstance(include_audio, bool):
        include_audio = True
    native = payload.get("video_quality") == "native"
    default_video = video_config_for_quality("native" if native else DEFAULT_VIDEO_QUALITY)
    renderer = payload.get("video_renderer", "auto")
    if renderer not in ("auto", "pixels", "texture"):
        renderer = "auto"
    try:
        video = VideoConfig(
            width=payload.get("video_width", default_video.width),
            height=payload.get("video_height", default_video.height),
            fps=payload.get("video_fps", default_video.fps),
            pixel_size=payload.get("video_pixel_size", default_video.pixel_size),
            renderer=renderer, native_resolution=native,
        )
        video.validate()
    except (TypeError, ValueError):
        video = video_config_for_quality(DEFAULT_VIDEO_QUALITY)
    video_stream = payload.get("video_stream", 0)
    if isinstance(video_stream, bool) or not isinstance(video_stream, int) or video_stream < 0:
        video_stream = 0
    return GuiSettings(
        last_input_dir=str(payload.get("last_input_dir") or ""),
        output_dir=str(payload.get("output_dir") or "output"),
        bank_output_dir=str(
            payload.get("bank_output_dir") or "output/device_banks"
        ),
        mode=mode,
        gain_db=gain_db,
        preserve_stereo=preserve_stereo,
        psychoacoustic_masking=masking,
        advanced_open=advanced_open,
        selected_profiles=selected or (mode,),
        video_enabled=video_enabled,
        video_width=video.width,
        video_height=video.height,
        video_fps=video.fps,
        video_pixel_size=video.pixel_size,
        video_stream=video_stream,
        include_audio=include_audio,
        video_quality=(
            "native" if video.native_resolution else
            "custom" if payload.get("video_quality") == "custom" else video_quality_name(video)
        ),
        video_renderer=video.renderer,
    )


def load_gui_settings(path: Path | None = None) -> GuiSettings:
    target = path or gui_settings_path()
    try:
        return _validated_settings(json.loads(target.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return GuiSettings()


def save_gui_settings(settings: GuiSettings, path: Path | None = None) -> Path:
    target = path or gui_settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
        delete=False,
    ) as temporary_file:
        temporary_file.write(content)
        temporary_file.flush()
        os.fsync(temporary_file.fileno())
        temporary = Path(temporary_file.name)
    try:
        temporary.replace(target)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return target


def inspect_device_pack(output_dir: Path, profile_name: str) -> PackStatus:
    profile = DEVICE_PROFILES[profile_name]
    expected_config = device_audio_config(AudioConfig(), profile)
    target = output_dir / f"wav2mc_{profile_name}_sine_bank.zip"
    if not target.is_file():
        return PackStatus("missing", target)
    try:
        with zipfile.ZipFile(target) as archive:
            metadata = json.loads(archive.read("wav2mc-bank.json"))
            pack_metadata = json.loads(archive.read("pack.mcmeta"))
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError):
        return PackStatus("mismatch", target)
    if not isinstance(metadata, dict) or not isinstance(pack_metadata, dict):
        return PackStatus("mismatch", target)
    pack = pack_metadata.get("pack")
    if not isinstance(pack, dict):
        return PackStatus("mismatch", target)
    expected = audio_config_metadata(expected_config)
    matches = (
        metadata.get("namespace") == f"wav2mc_{profile_name}"
        and metadata.get("device_profile") == profile_name
        and metadata.get("minecraft_version") == DEFAULT_MINECRAFT_VERSION
        and metadata.get("grain_level") == 1.0
        and pack.get("pack_format") == DEFAULT_RESOURCE_PACK_FORMAT
        and all(metadata.get(key) == value for key, value in expected.items())
    )
    return PackStatus("valid" if matches else "mismatch", target)
