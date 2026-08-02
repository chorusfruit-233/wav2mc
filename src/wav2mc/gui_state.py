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
    DEVICE_PROFILES,
    AudioConfig,
    audio_config_metadata,
    device_audio_config,
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
