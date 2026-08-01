import json
import zipfile
from pathlib import Path

from wav2mc.bank import build_device_pack_set
from wav2mc.config import DEVICE_PROFILES, AudioConfig, device_audio_config


def test_low_device_profile_reduces_bank_resolution() -> None:
    config = device_audio_config(AudioConfig(), DEVICE_PROFILES["low"])

    assert config.max_frequency == 2000
    assert config.frequency_step == 40
    assert config.phase_count == 8


def test_builds_device_tier_pack_set(tmp_path: Path) -> None:
    base_config = AudioConfig(
        min_frequency=400,
        max_frequency=480,
        frequency_step=20,
        phase_count=2,
    )

    outputs = build_device_pack_set(
        output_dir=tmp_path,
        base_config=base_config,
        pack_format=64,
    )
    manifest = json.loads(outputs["manifest"].read_text())

    assert set(outputs) == {"low", "normal", "high", "manifest"}
    assert manifest["profiles"]["low"]["sound_count"] == 6
    assert manifest["profiles"]["normal"]["sound_count"] == 10
    assert manifest["profiles"]["high"]["sound_count"] == 10

    for profile_name in ("low", "normal", "high"):
        target = outputs[profile_name]
        assert target.is_file()
        with zipfile.ZipFile(target) as archive:
            metadata = json.loads(archive.read("wav2mc-bank.json"))
        assert metadata["device_profile"] == profile_name
        assert metadata["namespace"] == f"wav2mc_{profile_name}"

    assert manifest["profiles"]["low"]["audio_config"]["frequency_step"] == 40
    assert manifest["profiles"]["high"]["audio_config"]["frequency_step"] == 20
