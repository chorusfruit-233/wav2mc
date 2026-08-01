from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QualityProfile:
    name: str
    max_components: int
    band_limits: tuple[tuple[int, int, int], ...]
    relative_floor_db: float


QUALITY_PROFILES: dict[str, QualityProfile] = {
    "low": QualityProfile(
        name="low",
        max_components=8,
        band_limits=((80, 500, 2), (500, 2000, 3), (2000, 4000, 3)),
        relative_floor_db=-34.0,
    ),
    "normal": QualityProfile(
        name="normal",
        max_components=12,
        band_limits=((80, 500, 3), (500, 2000, 5), (2000, 8000, 4)),
        relative_floor_db=-40.0,
    ),
    "high": QualityProfile(
        name="high",
        max_components=20,
        band_limits=((80, 500, 5), (500, 2000, 8), (2000, 8000, 7)),
        relative_floor_db=-46.0,
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


DEFAULT_RESOURCE_PACK_FORMAT = 64
DEFAULT_DATA_PACK_FORMAT = 81
DEFAULT_LAYOUT = "modern"
