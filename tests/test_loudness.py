import json
import zipfile
from pathlib import Path

import numpy as np

from wav2mc.analysis import AudioFrame, Component
from wav2mc.config import DEFAULT_DATA_PACK_FORMAT, LoudnessCalibration
from wav2mc.datapack import build_data_pack
from wav2mc.loudness import (
    maximum_reproducible_amplitude,
    minecraft_command_volume,
    predicted_minecraft_amplitude,
)


def test_loudness_calibration_round_trip() -> None:
    calibration = LoudnessCalibration(
        minecraft_gain=0.8,
        volume_exponent=1.4,
        max_command_volume=1.0,
    )
    target_amplitude = 0.24
    command_volume = minecraft_command_volume(
        target_amplitude,
        bank_grain_level=0.6,
        calibration=calibration,
    )

    predicted = predicted_minecraft_amplitude(
        command_volume,
        bank_grain_level=0.6,
        calibration=calibration,
    )
    assert np.isclose(predicted, target_amplitude)
    assert np.isclose(maximum_reproducible_amplitude(0.6, calibration), 0.48)


def test_data_pack_writes_calibrated_command_volume(tmp_path: Path) -> None:
    calibration = LoudnessCalibration(
        minecraft_gain=0.8,
        volume_exponent=2.0,
        max_command_volume=1.0,
    )
    frames = [
        AudioFrame(
            index=0,
            components=(Component(440, 0, 0.2),),
        )
    ]
    target = tmp_path / "calibrated.zip"
    build_data_pack(
        target,
        frames,
        namespace="calibrated",
        bank_namespace="wav2mc",
        pack_format=DEFAULT_DATA_PACK_FORMAT,
        layout="modern",
        bank_grain_level=0.5,
        loudness_calibration=calibration,
    )

    with zipfile.ZipFile(target) as archive:
        command = archive.read(
            "data/calibrated/function/frame/000000.mcfunction"
        ).decode()
        metadata = json.loads(archive.read("wav2mc-song.json"))
        pack_metadata = json.loads(archive.read("pack.mcmeta"))

    assert "0.707107 1.0 0.0" in command
    assert metadata["bank_grain_level"] == 0.5
    assert metadata["minecraft_version"] == "26.2"
    assert metadata["loudness_calibration"]["volume_exponent"] == 2.0
    assert pack_metadata["pack"]["min_format"] == [107, 1]
    assert pack_metadata["pack"]["max_format"] == [107, 1]
