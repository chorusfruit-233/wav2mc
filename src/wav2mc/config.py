from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class QualityProfile:
    name: str
    max_components: int
    band_limits: tuple[tuple[int, int, int], ...]
    relative_floor_db: float
    masking_offset_db: float = 10.0


QUALITY_PROFILES: dict[str, QualityProfile] = {
    "low": QualityProfile(
        name="low",
        max_components=8,
        band_limits=((80, 500, 2), (500, 2000, 3), (2000, 4000, 3)),
        relative_floor_db=-34.0,
        masking_offset_db=7.0,
    ),
    "normal": QualityProfile(
        name="normal",
        max_components=12,
        band_limits=((80, 500, 3), (500, 2000, 5), (2000, 8000, 4)),
        relative_floor_db=-40.0,
        masking_offset_db=10.0,
    ),
    "high": QualityProfile(
        name="high",
        max_components=20,
        band_limits=((80, 500, 5), (500, 2000, 8), (2000, 8000, 7)),
        relative_floor_db=-46.0,
        masking_offset_db=14.0,
    ),
}


@dataclass(frozen=True)
class AudioConfig:
    sample_rate: int = 48_000
    grain_ms: float = 100.0
    hop_ms: float = 50.0
    min_frequency: int = 80
    max_frequency: int = 8000
    frequency_step: int = 20
    phase_count: int = 16

    @property
    def window_size(self) -> int:
        return round(self.sample_rate * self.grain_ms / 1000.0)

    @property
    def hop_size(self) -> int:
        return round(self.sample_rate * self.hop_ms / 1000.0)

    @property
    def frequencies(self) -> tuple[int, ...]:
        return tuple(
            range(
                self.min_frequency,
                self.max_frequency + 1,
                self.frequency_step,
            )
        )


@dataclass(frozen=True)
class LoudnessCalibration:
    """Model Minecraft's gain and nonlinear `/playsound` volume response."""

    minecraft_gain: float = 1.0
    volume_exponent: float = 1.0
    max_command_volume: float = 1.0

    def validate(self) -> None:
        if self.minecraft_gain <= 0.0:
            raise ValueError("minecraft_gain must be positive")
        if self.volume_exponent <= 0.0:
            raise ValueError("volume_exponent must be positive")
        if self.max_command_volume <= 0.0:
            raise ValueError("max_command_volume must be positive")


@dataclass(frozen=True)
class DeviceProfile:
    name: str
    max_frequency: int
    minimum_frequency_step: int
    max_phase_count: int
    quality_name: str


DEVICE_PROFILES: dict[str, DeviceProfile] = {
    "low": DeviceProfile(
        name="low",
        max_frequency=2000,
        minimum_frequency_step=40,
        max_phase_count=8,
        quality_name="low",
    ),
    "normal": DeviceProfile(
        name="normal",
        max_frequency=4000,
        minimum_frequency_step=20,
        max_phase_count=8,
        quality_name="normal",
    ),
    "high": DeviceProfile(
        name="high",
        max_frequency=8000,
        minimum_frequency_step=20,
        max_phase_count=16,
        quality_name="high",
    ),
}


def device_audio_config(config: AudioConfig, profile: DeviceProfile) -> AudioConfig:
    max_frequency = min(config.max_frequency, profile.max_frequency)
    if max_frequency < config.min_frequency:
        raise ValueError(
            f"Device profile '{profile.name}' ends below the minimum frequency"
        )
    return replace(
        config,
        max_frequency=max_frequency,
        frequency_step=max(
            config.frequency_step,
            profile.minimum_frequency_step,
        ),
        phase_count=min(config.phase_count, profile.max_phase_count),
    )


DEFAULT_MINECRAFT_VERSION = "26.2"
DEFAULT_RESOURCE_PACK_FORMAT = 88.0
DEFAULT_DATA_PACK_FORMAT = 107.1
DEFAULT_LAYOUT = "modern"
