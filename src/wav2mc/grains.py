from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from io import BytesIO

import numpy as np
import soundfile as sf

from .audio import sqrt_hann, tonal_window
from .config import (
    AudioConfig, CHIRP_RATES, TRANSIENT_DELAYS_MS, TRANSIENT_SHAPES,
    VORBIS_COMPRESSION_LEVEL,
)


RESIDUAL_KINDS = ("noise", "transient")


def residual_event_name(
    kind: str, band_index: int, variant: int,
    delay_ms: int = 0, shape: str = "medium",
    polarity: int = 1,
) -> str:
    if kind not in RESIDUAL_KINDS:
        raise ValueError(f"Unknown residual kind: {kind}")
    name = f"{kind}.b{band_index:02d}.v{variant:02d}"
    if delay_ms or shape != "medium":
        name += f".d{delay_ms:02d}.{shape}"
    if polarity < 0:
        name += ".inv"
    return name


def tonal_event_name(
    frequency: int, phase_index: int, chirp_rate: int = 0,
    envelope: str = "hann",
) -> str:
    name = f"grain.f{frequency:04d}.p{phase_index:02d}"
    if chirp_rate or envelope != "hann":
        name += f".c{chirp_rate}.{envelope}"
    return name


def tonal_variants(
    frequency: int, sample_rate: int, window_size: int,
) -> Iterator[tuple[int, str]]:
    """Only generate sweeps whose instantaneous frequencies stay in band."""
    half_duration = window_size / (2 * sample_rate)
    for rate in CHIRP_RATES:
        excursion = abs(rate) * half_duration
        if excursion < frequency and frequency + excursion < sample_rate / 2:
            yield rate, "hann"
    for envelope in ("start", "end", "both"):
        yield 0, envelope


def residual_variants(kind: str) -> Iterator[tuple[int, str, int]]:
    if kind == "noise":
        yield 0, "medium", 1
        yield 0, "medium", -1
    else:
        for delay in TRANSIENT_DELAYS_MS:
            for shape in TRANSIENT_SHAPES:
                yield delay, shape, 1


def bank_sound_counts(config: AudioConfig) -> tuple[int, int]:
    tonal = config.phase_count * sum(
        len(tuple(tonal_variants(f, config.sample_rate, config.window_size)))
        for f in config.frequencies
    )
    residual = len(config.residual_bands) * config.residual_variant_count * sum(
        len(tuple(residual_variants(kind))) for kind in RESIDUAL_KINDS
    )
    return tonal, residual


@lru_cache(maxsize=512)
def tonal_grain(
    sample_rate: int, window_size: int, frequency: int, phase_index: int,
    phase_count: int, chirp_rate: int = 0, envelope: str = "hann",
) -> np.ndarray:
    time = np.arange(window_size, dtype=np.float64) / sample_rate
    window = tonal_window(window_size).copy()
    if envelope in ("start", "both"):
        window[:window_size // 2] = 1.0
    if envelope in ("end", "both"):
        window[window_size // 2:] = 1.0
    phase = 2 * np.pi * phase_index / phase_count
    cycles = (frequency - chirp_rate * window_size / (2 * sample_rate)) * time
    cycles += 0.5 * chirp_rate * time ** 2
    return (window * np.cos(2 * np.pi * cycles + phase)).astype(np.float32)


def encode_ogg(audio: np.ndarray, sample_rate: int) -> bytes:
    target = BytesIO()
    sf.write(
        target, audio, sample_rate, format="OGG", subtype="VORBIS",
        compression_level=VORBIS_COMPRESSION_LEVEL,
    )
    return target.getvalue()


def codec_round_trip(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    decoded, _ = sf.read(BytesIO(encode_ogg(audio, sample_rate)), dtype="float32")
    return decoded


@lru_cache(maxsize=512)
def residual_grain(
    sample_rate: int,
    window_size: int,
    band_index: int,
    low_frequency: int,
    high_frequency: int,
    variant: int,
    kind: str,
    delay_ms: int = 0,
    shape: str = "medium",
    polarity: int = 1,
) -> np.ndarray:
    if kind not in RESIDUAL_KINDS:
        raise ValueError(f"Unknown residual kind: {kind}")
    if shape not in TRANSIENT_SHAPES or not 0 <= delay_ms < 50:
        raise ValueError("Invalid transient shape or delay")
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
        attack_ms, decay_ms = {
            "fast": (0.3, 6.0), "medium": (0.8, 18.0), "slow": (2.0, 40.0),
        }[shape]
        attack = 1.0 - np.exp(-time / (attack_ms / 1000))
        decay = np.exp(-time / (decay_ms / 1000))
        envelope = attack * decay
    values *= envelope

    peak = float(np.max(np.abs(values), initial=0.0))
    if peak <= 1e-12:
        return np.zeros(window_size, dtype=np.float32)
    values = (values * (0.98 / peak)).astype(np.float32)
    if delay_ms:
        delay = round(sample_rate * delay_ms / 1000)
        # Keep the entire decay; the next frame may overlap this delayed grain.
        values = np.pad(values, (delay, 0))
    return values * polarity


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
    delay_ms: int = 0,
    shape: str = "medium",
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
            delay_ms,
            shape,
        )
    )
