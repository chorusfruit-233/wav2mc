from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class QualityProfile:
    name: str
    max_components: int
    band_limits: tuple[tuple[int, int, int], ...]
    relative_floor_db: float
    masking_offset_db: float = 10.0
    max_noise_components: int = 0
    max_transient_components: int = 0
    residual_floor_db: float = -40.0


QUALITY_PROFILES: dict[str, QualityProfile] = {
    "low": QualityProfile(
        name="low",
        max_components=8,
        band_limits=((80, 500, 2), (500, 2000, 3), (2000, 4000, 3)),
        relative_floor_db=-34.0,
        masking_offset_db=7.0,
        max_noise_components=1,
        max_transient_components=1,
        residual_floor_db=-30.0,
    ),
    "voice": QualityProfile(
        name="voice",
        max_components=12,
        band_limits=((80, 500, 3), (500, 2000, 4), (2000, 8001, 5)),
        relative_floor_db=-38.0,
        masking_offset_db=9.0,
        max_noise_components=2,
        max_transient_components=2,
        residual_floor_db=-34.0,
    ),
    "normal": QualityProfile(
        name="normal",
        max_components=20,
        band_limits=(
            (60, 500, 4),
            (500, 2000, 6),
            (2000, 8000, 7),
            (8000, 12001, 3),
        ),
        relative_floor_db=-44.0,
        masking_offset_db=10.0,
        max_noise_components=4,
        max_transient_components=4,
        residual_floor_db=-38.0,
    ),
    "high": QualityProfile(
        name="high",
        max_components=24,
        band_limits=(
            (40, 500, 5),
            (500, 2000, 7),
            (2000, 8000, 8),
            (8000, 16001, 4),
        ),
        relative_floor_db=-48.0,
        masking_offset_db=14.0,
        max_noise_components=6,
        max_transient_components=6,
        residual_floor_db=-42.0,
    ),
    "experimental": QualityProfile(
        name="experimental",
        max_components=32,
        band_limits=(
            (20, 500, 6),
            (500, 2000, 9),
            (2000, 8000, 12),
            (8000, 20001, 5),
        ),
        relative_floor_db=-52.0,
        masking_offset_db=16.0,
        max_noise_components=8,
        max_transient_components=8,
        residual_floor_db=-46.0,
    ),
}


ADAPTIVE_FREQUENCY_BANDS: tuple[tuple[int, int, int], ...] = (
    (20, 1000, 20),
    (1000, 4000, 40),
    (4000, 8000, 80),
    (8000, 12000, 160),
    (12000, 20000, 320),
)


RESIDUAL_FREQUENCY_BANDS: tuple[tuple[int, int], ...] = (
    (20, 80),
    (80, 160),
    (160, 315),
    (315, 500),
    (500, 800),
    (800, 1250),
    (1250, 2000),
    (2000, 3150),
    (3150, 5000),
    (5000, 6300),
    (6300, 8000),
    (8000, 10000),
    (10000, 12500),
    (12500, 15000),
    (15000, 17500),
    (17500, 20001),
)


@dataclass(frozen=True)
class AudioConfig:
    sample_rate: int = 48_000
    grain_ms: float = 100.0
    hop_ms: float = 50.0
    min_frequency: int = 20
    max_frequency: int = 20000
    frequency_step: int = 20
    phase_count: int = 16
    adaptive_frequency_grid: bool = True
    hybrid_residual: bool = True
    residual_variant_count: int = 4

    @property
    def window_size(self) -> int:
        return round(self.sample_rate * self.grain_ms / 1000.0)

    @property
    def hop_size(self) -> int:
        return round(self.sample_rate * self.hop_ms / 1000.0)

    @property
    def frequency_grid(self) -> str:
        return "adaptive" if self.adaptive_frequency_grid else "uniform"

    @property
    def frequency_bands(self) -> tuple[tuple[int, int, int], ...]:
        if not self.adaptive_frequency_grid:
            return ((self.min_frequency, self.max_frequency, self.frequency_step),)
        bands: list[tuple[int, int, int]] = []
        seen: set[int] = set()
        for low, high, step in ADAPTIVE_FREQUENCY_BANDS:
            start = max(self.min_frequency, low)
            stop = min(self.max_frequency, high)
            first = low + ((start - low + step - 1) // step) * step
            values = [
                frequency
                for frequency in range(first, stop + 1, step)
                if frequency not in seen
            ]
            if values:
                bands.append((values[0], values[-1], step))
                seen.update(values)
        return tuple(bands)

    @property
    def frequencies(self) -> tuple[int, ...]:
        if not self.adaptive_frequency_grid:
            return tuple(
                range(
                    self.min_frequency,
                    self.max_frequency + 1,
                    self.frequency_step,
                )
            )

        return tuple(
            frequency
            for low, high, step in self.frequency_bands
            for frequency in range(low, high + 1, step)
        )

    @property
    def residual_bands(self) -> tuple[tuple[int, int, int], ...]:
        if not self.hybrid_residual:
            return ()
        bands = []
        for index, (low, high) in enumerate(RESIDUAL_FREQUENCY_BANDS):
            clipped_low = max(low, self.min_frequency)
            clipped_high = min(high, self.max_frequency + 1)
            if clipped_low < clipped_high:
                bands.append((index, clipped_low, clipped_high))
        return tuple(bands)


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
    min_frequency: int
    max_frequency: int
    minimum_frequency_step: int
    max_phase_count: int
    quality_name: str
    adaptive_frequency_grid: bool = True


DEVICE_PROFILES: dict[str, DeviceProfile] = {
    "low": DeviceProfile(
        name="low",
        min_frequency=80,
        max_frequency=2000,
        minimum_frequency_step=40,
        max_phase_count=8,
        quality_name="low",
        adaptive_frequency_grid=False,
    ),
    "voice": DeviceProfile(
        name="voice",
        min_frequency=80,
        max_frequency=8000,
        minimum_frequency_step=20,
        max_phase_count=8,
        quality_name="voice",
    ),
    "normal": DeviceProfile(
        name="normal",
        min_frequency=60,
        max_frequency=12000,
        minimum_frequency_step=20,
        max_phase_count=12,
        quality_name="normal",
    ),
    "high": DeviceProfile(
        name="high",
        min_frequency=40,
        max_frequency=16000,
        minimum_frequency_step=20,
        max_phase_count=16,
        quality_name="high",
    ),
    "experimental": DeviceProfile(
        name="experimental",
        min_frequency=20,
        max_frequency=20000,
        minimum_frequency_step=20,
        max_phase_count=16,
        quality_name="experimental",
    ),
}


DEFAULT_DEVICE_PACK_PROFILES = ("voice", "normal", "high", "experimental")


def device_audio_config(config: AudioConfig, profile: DeviceProfile) -> AudioConfig:
    min_frequency = max(config.min_frequency, profile.min_frequency)
    max_frequency = min(config.max_frequency, profile.max_frequency)
    if max_frequency < min_frequency:
        raise ValueError(
            f"Device profile '{profile.name}' does not overlap the frequency range"
        )
    return replace(
        config,
        min_frequency=min_frequency,
        max_frequency=max_frequency,
        frequency_step=max(
            config.frequency_step,
            profile.minimum_frequency_step,
        ),
        phase_count=min(config.phase_count, profile.max_phase_count),
        adaptive_frequency_grid=profile.adaptive_frequency_grid,
    )


def audio_config_metadata(config: AudioConfig) -> dict[str, object]:
    return {
        "sample_rate": config.sample_rate,
        "grain_ms": config.grain_ms,
        "hop_ms": config.hop_ms,
        "min_frequency": config.min_frequency,
        "max_frequency": config.max_frequency,
        "frequency_grid": config.frequency_grid,
        "frequency_step": config.frequency_step,
        "frequency_bands": [
            {
                "min_frequency": low,
                "max_frequency": high,
                "frequency_step": step,
            }
            for low, high, step in config.frequency_bands
        ],
        "frequency_count": len(config.frequencies),
        "phase_count": config.phase_count,
        "hybrid_residual": config.hybrid_residual,
        "residual_variant_count": config.residual_variant_count,
        "residual_bands": [
            {
                "band_index": index,
                "min_frequency": low,
                "max_frequency": high - 1,
            }
            for index, low, high in config.residual_bands
        ],
    }


DEFAULT_MINECRAFT_VERSION = "26.2"
DEFAULT_RESOURCE_PACK_FORMAT = 88.0
DEFAULT_DATA_PACK_FORMAT = 107.1
DEFAULT_LAYOUT = "modern"
