from __future__ import annotations

from functools import lru_cache

import numpy as np

from .analysis import AudioFrame
from .audio import sqrt_hann
from .config import AudioConfig
from .grains import residual_grain


def synthesize_preview(frames: list[AudioFrame], config: AudioConfig) -> np.ndarray:
    n = config.window_size
    hop = config.hop_size
    window = sqrt_hann(n)
    sample_positions = np.arange(n, dtype=np.float64) / config.sample_rate

    @lru_cache(maxsize=1024)
    def grain(frequency: int, phase_index: int) -> np.ndarray:
        phase = 2.0 * np.pi * phase_index / config.phase_count
        values = window * np.cos(2.0 * np.pi * frequency * sample_positions + phase)
        return values.astype(np.float32)

    @lru_cache(maxsize=256)
    def residual(
        kind: str,
        band_index: int,
        low_frequency: int,
        high_frequency: int,
        variant: int,
    ) -> np.ndarray:
        return residual_grain(
            config.sample_rate,
            n,
            band_index,
            low_frequency,
            high_frequency,
            variant,
            kind,
        )

    output_size = max(n, (max(0, len(frames) - 1) * hop) + n)
    output = np.zeros(output_size, dtype=np.float32)

    for frame in frames:
        start = frame.index * hop
        end = start + n
        for component in frame.components:
            output[start:end] += component.amplitude * grain(
                component.frequency,
                component.phase_index,
            )
        for component in frame.residual_components:
            output[start:end] += component.amplitude * residual(
                component.kind,
                component.band_index,
                component.low_frequency,
                component.high_frequency,
                component.variant,
            )

    return output


def calculate_safe_scale(
    preview: np.ndarray,
    target_peak: float = 0.88,
    requested_gain: float = 1.0,
) -> float:
    peak = float(np.max(np.abs(preview))) if preview.size else 0.0
    if peak <= 1e-12:
        return requested_gain
    return min(requested_gain, target_peak / peak)
