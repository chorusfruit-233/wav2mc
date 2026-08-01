import json
import zipfile
from pathlib import Path

from wav2mc.bank import build_device_pack_set
from wav2mc.config import (
    DEVICE_PROFILES,
    DEFAULT_RESOURCE_PACK_FORMAT,
    AudioConfig,
    device_audio_config,
)


def test_low_device_profile_reduces_bank_resolution() -> None:
    config = device_audio_config(AudioConfig(), DEVICE_PROFILES["low"])

    assert config.max_frequency == 2000
    assert config.frequency_step == 40
    assert config.phase_count == 8
    assert config.frequency_grid == "uniform"


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
        pack_format=DEFAULT_RESOURCE_PACK_FORMAT,
    )
    manifest = json.loads(outputs["manifest"].read_text())

    expected_profiles = {"voice", "normal", "high", "experimental"}
    assert set(outputs) == expected_profiles | {"manifest"}
    assert manifest["minecraft_version"] == "26.2"
    assert manifest["pack_format"] == 88.0
    assert manifest["profiles"]["voice"]["sound_count"] == 18
    assert manifest["profiles"]["normal"]["sound_count"] == 18
    assert manifest["profiles"]["high"]["sound_count"] == 18
    assert manifest["profiles"]["experimental"]["sound_count"] == 18

    for profile_name in expected_profiles:
        target = outputs[profile_name]
        assert target.is_file()
        with zipfile.ZipFile(target) as archive:
            metadata = json.loads(archive.read("wav2mc-bank.json"))
            pack_metadata = json.loads(archive.read("pack.mcmeta"))
        assert metadata["device_profile"] == profile_name
        assert metadata["minecraft_version"] == "26.2"
        assert metadata["namespace"] == f"wav2mc_{profile_name}"
        assert metadata["frequency_grid"] == "adaptive"
        assert metadata["hybrid_residual"] is True
        assert len(metadata["residual_bands"]) == 1
        assert pack_metadata["pack"]["pack_format"] == 88.0
        assert pack_metadata["pack"]["min_format"] == [88, 0]
        assert pack_metadata["pack"]["max_format"] == [88, 0]

    assert manifest["profiles"]["voice"]["quality"] == "voice"
    assert manifest["profiles"]["experimental"]["quality"] == "experimental"
    assert manifest["profiles"]["normal"]["residual_sound_count"] == 8
