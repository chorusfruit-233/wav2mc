from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from wav2mc.analysis import analyse_audio
from wav2mc.audio import preprocess_audio, probe_media
from wav2mc.bank import build_resource_pack
from wav2mc.config import (
    DEFAULT_DATA_PACK_FORMAT,
    DEFAULT_LAYOUT,
    DEFAULT_RESOURCE_PACK_FORMAT,
    DEVICE_PROFILES,
    QUALITY_PROFILES,
    AudioConfig,
    audio_config_metadata,
    device_audio_config,
)
from wav2mc.datapack import build_data_pack
from wav2mc.gui_state import (
    GuiSettings,
    inspect_device_pack,
    load_gui_settings,
    save_gui_settings,
)
from wav2mc.pipeline import convert_audio
from wav2mc.utils import TaskCancelled


def _small_config() -> AudioConfig:
    return AudioConfig(
        sample_rate=8_000,
        min_frequency=100,
        max_frequency=500,
        frequency_step=100,
        phase_count=2,
        adaptive_frequency_grid=False,
        hybrid_residual=False,
    )


def _write_tone(path: Path, sample_rate: int = 8_000) -> None:
    positions = np.arange(sample_rate // 4) / sample_rate
    audio = 0.5 * np.cos(2.0 * np.pi * 400.0 * positions)
    sf.write(path, audio, sample_rate, subtype="PCM_16")


def test_probe_media_parses_audio_stream_ordinals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.mkv"
    source.write_bytes(b"media")
    payload = {
        "format": {"duration": "12.5", "format_name": "matroska,webm"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264"},
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
                "duration": "12.4",
            },
            {
                "codec_type": "audio",
                "codec_name": "opus",
                "sample_rate": "invalid",
                "channels": None,
            },
        ],
    }
    result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
    )
    monkeypatch.setattr("wav2mc.audio.ensure_command", lambda _name: "ffprobe")
    monkeypatch.setattr("wav2mc.audio.subprocess.run", lambda *_a, **_kw: result)

    info = probe_media(source)

    assert info.duration == pytest.approx(12.5)
    assert info.format_name == "matroska,webm"
    assert [stream.audio_index for stream in info.streams] == [0, 1]
    assert info.streams[0].codec == "aac"
    assert info.streams[0].sample_rate == 48_000
    assert info.streams[1].sample_rate == 0
    assert info.streams[1].channels == 0


def test_gui_settings_validate_and_save_atomically(tmp_path: Path) -> None:
    path = tmp_path / "config" / "gui.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "mode": "experimental",
                "gain_db": "nan",
                "preserve_stereo": "false",
                "selected_profiles": "voice",
            }
        ),
        encoding="utf-8",
    )

    settings = load_gui_settings(path)

    assert settings.mode == "experimental"
    assert settings.gain_db == 0.0
    assert settings.preserve_stereo is True
    assert settings.selected_profiles == ("experimental",)

    expected = GuiSettings(
        output_dir="custom-output",
        selected_profiles=("voice", "high"),
    )
    save_gui_settings(expected, path)

    assert load_gui_settings(path) == expected
    assert not list(path.parent.glob("*.tmp"))

    path.write_text("{broken", encoding="utf-8")
    assert load_gui_settings(path) == GuiSettings()


def test_pack_status_recognizes_valid_and_mismatched_metadata(tmp_path: Path) -> None:
    profile = DEVICE_PROFILES["normal"]
    config = device_audio_config(AudioConfig(), profile)
    target = tmp_path / "wav2mc_normal_sine_bank.zip"
    metadata = {
        "minecraft_version": "26.2",
        "namespace": "wav2mc_normal",
        "device_profile": "normal",
        "grain_level": 1.0,
        **audio_config_metadata(config),
    }
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("wav2mc-bank.json", json.dumps(metadata))
        archive.writestr(
            "pack.mcmeta",
            json.dumps({"pack": {"pack_format": DEFAULT_RESOURCE_PACK_FORMAT}}),
        )

    assert inspect_device_pack(tmp_path, "normal").state == "valid"

    metadata["phase_count"] = 2
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("wav2mc-bank.json", json.dumps(metadata))
        archive.writestr(
            "pack.mcmeta",
            json.dumps({"pack": {"pack_format": DEFAULT_RESOURCE_PACK_FORMAT}}),
        )
    assert inspect_device_pack(tmp_path, "normal").state == "mismatch"
    assert inspect_device_pack(tmp_path, "voice").state == "missing"


def test_conversion_progress_is_monotonic_and_finishes_at_one(tmp_path: Path) -> None:
    source = tmp_path / "tone.wav"
    _write_tone(source)
    updates = []

    outputs = convert_audio(
        source=source,
        output_dir=tmp_path / "output",
        song_name="progress",
        config=_small_config(),
        quality=QUALITY_PROFILES["low"],
        data_pack_format=DEFAULT_DATA_PACK_FORMAT,
        layout=DEFAULT_LAYOUT,
        bank_namespace="wav2mc_test",
        category="record",
        requested_gain=1.0,
        bank_grain_level=1.0,
        progress_callback=updates.append,
    )

    fractions = [update.fraction for update in updates]
    assert fractions == sorted(fractions)
    assert fractions[-1] == 1.0
    assert {"decode", "analyse", "reconstruct", "datapack", "report"} <= {
        update.stage for update in updates
    }
    report = json.loads(outputs["report"].read_text(encoding="utf-8"))
    assert report["actual_playsound_command_count"] >= 0


def test_conversion_cancel_during_datapack_preserves_outputs(tmp_path: Path) -> None:
    source = tmp_path / "tone.wav"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _write_tone(source)
    final_paths = {
        "data_pack": output_dir / "cancel_datapack.zip",
        "preview": output_dir / "cancel_preview.wav",
        "report": output_dir / "cancel_analysis.json",
    }
    for path in final_paths.values():
        path.write_bytes(b"existing")
    cancel = False

    def progress(update: object) -> None:
        nonlocal cancel
        if getattr(update, "stage", None) == "datapack":
            cancel = True

    with pytest.raises(TaskCancelled):
        convert_audio(
            source=source,
            output_dir=output_dir,
            song_name="cancel",
            config=_small_config(),
            quality=QUALITY_PROFILES["low"],
            data_pack_format=DEFAULT_DATA_PACK_FORMAT,
            layout=DEFAULT_LAYOUT,
            bank_namespace="wav2mc_test",
            category="record",
            requested_gain=1.0,
            bank_grain_level=1.0,
            progress_callback=progress,
            cancel_check=lambda: cancel,
        )

    assert all(path.read_bytes() == b"existing" for path in final_paths.values())
    assert not list(output_dir.glob(".wav2mc-convert-*"))


def test_decode_analysis_and_pack_cancellation_are_cooperative(tmp_path: Path) -> None:
    source = tmp_path / "tone.wav"
    _write_tone(source)
    decoded = tmp_path / "decoded.wav"
    decoded.write_bytes(b"existing")

    with pytest.raises(TaskCancelled):
        preprocess_audio(
            source,
            decoded,
            sample_rate=8_000,
            low_frequency=80,
            high_frequency=1_000,
            cancel_check=lambda: True,
        )
    assert decoded.read_bytes() == b"existing"

    audio = np.zeros(8_000, dtype=np.float32)
    with pytest.raises(TaskCancelled):
        analyse_audio(
            audio,
            _small_config(),
            QUALITY_PROFILES["low"],
            cancel_check=lambda: True,
        )

    pack = tmp_path / "pack.zip"
    pack.write_bytes(b"existing")
    with pytest.raises(TaskCancelled):
        build_resource_pack(
            output=pack,
            config=_small_config(),
            pack_format=DEFAULT_RESOURCE_PACK_FORMAT,
            cancel_check=lambda: True,
        )
    assert pack.read_bytes() == b"existing"

    data_pack = tmp_path / "data.zip"
    data_pack.write_bytes(b"existing")
    with pytest.raises(TaskCancelled):
        build_data_pack(
            data_pack,
            frames=[],
            namespace="cancel",
            bank_namespace="wav2mc_test",
            pack_format=DEFAULT_DATA_PACK_FORMAT,
            layout=DEFAULT_LAYOUT,
            cancel_check=lambda: True,
        )
    assert data_pack.read_bytes() == b"existing"
