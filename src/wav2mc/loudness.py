from __future__ import annotations

from .config import LoudnessCalibration


def maximum_reproducible_amplitude(
    bank_grain_level: float,
    calibration: LoudnessCalibration,
) -> float:
    calibration.validate()
    if not 0.0 < bank_grain_level <= 1.0:
        raise ValueError("bank_grain_level must be in (0, 1]")
    return (
        bank_grain_level
        * calibration.minecraft_gain
        * calibration.max_command_volume**calibration.volume_exponent
    )


def predicted_minecraft_amplitude(
    command_volume: float,
    bank_grain_level: float,
    calibration: LoudnessCalibration,
) -> float:
    calibration.validate()
    if command_volume < 0.0:
        raise ValueError("command_volume must not be negative")
    return (
        bank_grain_level
        * calibration.minecraft_gain
        * command_volume**calibration.volume_exponent
    )


def minecraft_command_volume(
    target_amplitude: float,
    bank_grain_level: float,
    calibration: LoudnessCalibration,
) -> float:
    if target_amplitude < 0.0:
        raise ValueError("target_amplitude must not be negative")
    maximum = maximum_reproducible_amplitude(bank_grain_level, calibration)
    if target_amplitude >= maximum:
        return calibration.max_command_volume
    normalized = target_amplitude / (
        bank_grain_level * calibration.minecraft_gain
    )
    return normalized ** (1.0 / calibration.volume_exponent)
