from __future__ import annotations

from functools import lru_cache

import numpy as np

from .audio import sqrt_hann


RESIDUAL_KINDS = ("noise", "transient")


def residual_event_name(kind: str, band_index: int, variant: int) -> str:
    if kind not in RESIDUAL_KINDS:
        raise ValueError(f"Unknown residual kind: {kind}")
    return f"{kind}.b{band_index:02d}.v{variant:02d}"


@lru_cache(maxsize=512)
def residual_grain(
    sample_rate: int,
    window_size: int,
    band_index: int,
    low_frequency: int,
    high_frequency: int,
    variant: int,
    kind: str,
) -> np.ndarray:
    if kind not in RESIDUAL_KINDS:
        raise ValueError(f"Unknown residual kind: {kind}")
    if not 0 <= low_frequency < high_frequency <= sample_rate // 2 + 1:
        raise ValueError("Invalid residual frequency band")

    seed = (
        0x5F3759DF
        + band_index * 1009
        + variant * 9176
        + (0 if kind == "noise" else 104729)
    )
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(window_size)
    spectrum = np.fft.rfft(white)
    frequencies = np.fft.rfftfreq(window_size, 1.0 / sample_rate)

    bandwidth = max(20.0, high_frequency - low_frequency)
    transition = min(80.0, bandwidth * 0.15)
    response = np.zeros_like(frequencies)
    core = (frequencies >= low_frequency) & (frequencies < high_frequency)
    response[core] = 1.0
    if transition > 0.0:
        lower = (frequencies >= max(0.0, low_frequency - transition)) & (
            frequencies < low_frequency
        )
        upper = (frequencies >= high_frequency) & (
            frequencies < high_frequency + transition
        )
        response[lower] = 0.5 - 0.5 * np.cos(
            np.pi
            * (frequencies[lower] - (low_frequency - transition))
            / transition
        )
        response[upper] = 0.5 + 0.5 * np.cos(
            np.pi * (frequencies[upper] - high_frequency) / transition
        )

    values = np.fft.irfft(spectrum * response, n=window_size)
    if kind == "noise":
        envelope = sqrt_hann(window_size).astype(np.float64)
    else:
        time = np.arange(window_size, dtype=np.float64) / sample_rate
        attack = 1.0 - np.exp(-time / 0.0008)
        decay = np.exp(-time / 0.018)
        envelope = attack * decay
    values *= envelope

    peak = float(np.max(np.abs(values), initial=0.0))
    if peak <= 1e-12:
        return np.zeros(window_size, dtype=np.float32)
    return (values * (0.98 / peak)).astype(np.float32)


def residual_grain_rms(grain: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.asarray(grain, dtype=np.float64) ** 2)))


@lru_cache(maxsize=512)
def residual_grain_reference_rms(
    sample_rate: int,
    window_size: int,
    band_index: int,
    low_frequency: int,
    high_frequency: int,
    variant: int,
    kind: str,
) -> float:
    return residual_grain_rms(
        residual_grain(
            sample_rate,
            window_size,
            band_index,
            low_frequency,
            high_frequency,
            variant,
            kind,
        )
    )
