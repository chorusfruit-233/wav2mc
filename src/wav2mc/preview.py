from __future__ import annotations

import json
import zipfile
from contextlib import contextmanager
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterator

import numpy as np
import soundfile as sf

from .analysis import AudioFrame
from .config import AudioConfig, audio_config_metadata
from .grains import (
    codec_round_trip, residual_event_name, residual_grain,
    tonal_event_name, tonal_grain,
)
from .utils import (
    CancelCheck,
    ProgressCallback,
    check_cancelled,
    emit_progress,
)


GrainLookup = Callable[[str], np.ndarray]


@contextmanager
def open_preview_bank(
    path: Path, config: AudioConfig, namespace: str, grain_level: float,
) -> Iterator[GrainLookup]:
    """Read the exact Vorbis samples from a compatible resource pack."""
    try:
        with zipfile.ZipFile(path) as archive:
            metadata = json.loads(archive.read("wav2mc-bank.json"))
            expected = audio_config_metadata(config) | {
                "namespace": namespace, "grain_level": grain_level,
            }
            if not isinstance(metadata, dict) or any(
                metadata.get(key) != value for key, value in expected.items()
            ):
                raise ValueError("Preview resource pack parameters do not match; rebuild the bank")
            sounds = json.loads(archive.read(f"assets/{namespace}/sounds.json"))

            @lru_cache(maxsize=512)
            def lookup(event: str) -> np.ndarray:
                asset = sounds[event]["sounds"][0]["name"]
                asset_namespace, relative = asset.split(":", 1)
                content = archive.read(f"assets/{asset_namespace}/sounds/{relative}.ogg")
                audio, sample_rate = sf.read(BytesIO(content), dtype="float32")
                if sample_rate != config.sample_rate or audio.ndim != 1:
                    raise ValueError("Preview resource pack contains incompatible audio")
                return audio / grain_level

            try:
                yield lookup
            finally:
                lookup.cache_clear()
    except (KeyError, TypeError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid preview resource pack: {path}") from exc


def _is_stereo(frames: list[AudioFrame]) -> bool:
    return any(
        component.pan != 0.0
        for frame in frames
        for component in frame.all_components
    )


def _add_panned(
    target: np.ndarray,
    values: np.ndarray,
    pan: float,
) -> None:
    if target.ndim == 1:
        target += values
    elif pan < 0.0:
        target[:, 0] += values
    elif pan > 0.0:
        target[:, 1] += values
    else:
        target[:, 0] += values
        target[:, 1] += values


@lru_cache(maxsize=512)
def decoded_tonal_grain(
    config: AudioConfig, frequency: int, phase_index: int,
    chirp_rate: int, envelope: str, grain_level: float,
) -> np.ndarray:
    samples = tonal_grain(config.sample_rate, config.window_size, frequency,
                          phase_index, config.phase_count, chirp_rate, envelope)
    return codec_round_trip(grain_level * samples, config.sample_rate) / grain_level


@lru_cache(maxsize=512)
def decoded_residual_grain(args: tuple, grain_level: float) -> np.ndarray:
    return codec_round_trip(grain_level * residual_grain(*args), args[0]) / grain_level


def residual_frame_size(frame: AudioFrame, config: AudioConfig) -> int:
    return config.window_size + max(
        (round(c.delay_ms * config.sample_rate / 1000) for c in frame.residual_components),
        default=0,
    )


def preview_size(frames: list[AudioFrame], config: AudioConfig) -> int:
    return max([config.window_size] + [
        frame.index * config.hop_size + max(
            residual_frame_size(frame, config),
            max((int(np.ceil(config.window_size / c.pitch)) for c in frame.components),
                default=config.window_size),
        ) for frame in frames
    ])


def synthesize_preview(
    frames: list[AudioFrame],
    config: AudioConfig,
    stereo: bool | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    encoded: bool = False,
    bank_grain_level: float = 1.0,
    bank_lookup: GrainLookup | None = None,
) -> np.ndarray:
    n = config.window_size
    hop = config.hop_size

    @lru_cache(maxsize=128)
    def grain(
        frequency: int, phase_index: int, pitch: float,
        chirp_rate: int, envelope_name: str,
    ) -> np.ndarray:
        positions = np.arange(int(np.ceil(n / pitch)), dtype=np.float64) * pitch
        if encoded or bank_lookup is not None:
            if bank_lookup is not None:
                samples = bank_lookup(tonal_event_name(
                    frequency, phase_index, chirp_rate, envelope_name,
                ))
            else:
                samples = decoded_tonal_grain(
                    config, frequency, phase_index, chirp_rate, envelope_name,
                    bank_grain_level,
                )
            return np.interp(positions, np.arange(n), samples, right=0).astype(np.float32)
        envelope = 0.5 - 0.5 * np.cos(2 * np.pi * positions / (n - 1))
        if envelope_name in ("start", "both"):
            envelope[positions < n // 2] = 1.0
        if envelope_name in ("end", "both"):
            envelope[positions >= n // 2] = 1.0
        envelope[positions > n - 1] = 0.0
        time = positions / config.sample_rate
        cycles = (
            (frequency - chirp_rate * n / (2 * config.sample_rate)) * time
            + 0.5 * chirp_rate * time ** 2
        )
        phase = 2 * np.pi * phase_index / config.phase_count
        return (envelope * np.cos(2 * np.pi * cycles + phase)).astype(np.float32)

    output_size = preview_size(frames, config)
    if stereo is None:
        stereo = _is_stereo(frames)
    output_shape = (output_size, 2) if stereo else output_size
    output = np.zeros(output_shape, dtype=np.float32)

    emit_progress(progress_callback, "reconstruct", 0.0, "Reconstructing preview")
    for position, frame in enumerate(frames):
        if position % 32 == 0:
            check_cancelled(cancel_check)
            emit_progress(
                progress_callback,
                "reconstruct",
                position / max(1, len(frames)),
                "Reconstructing preview",
            )
        start = frame.index * hop
        for component in frame.components:
            values = grain(component.frequency, component.phase_index, component.pitch,
                           component.chirp_rate, component.envelope)
            _add_panned(
                output[start:start + values.size],
                component.amplitude * values,
                component.pan,
            )
        residual = synthesize_residual_frame(
            frame,
            config,
            stereo=stereo,
            encoded=encoded,
            bank_grain_level=bank_grain_level,
            bank_lookup=bank_lookup,
        )
        output[start:start + len(residual)] += residual

    check_cancelled(cancel_check)
    emit_progress(progress_callback, "reconstruct", 1.0, "Preview reconstructed")
    return output


def synthesize_residual_frame(
    frame: AudioFrame,
    config: AudioConfig,
    kind: str | None = None,
    stereo: bool | None = None,
    encoded: bool = False,
    bank_grain_level: float = 1.0,
    bank_lookup: GrainLookup | None = None,
) -> np.ndarray:
    if stereo is None:
        stereo = any(component.pan != 0.0 for component in frame.all_components)
    size = residual_frame_size(frame, config)
    output_shape = (size, 2) if stereo else size
    output = np.zeros(output_shape, dtype=np.float32)
    for component in frame.residual_components:
        if kind is not None and component.kind != kind:
            continue
        args = (
            config.sample_rate, config.window_size, component.band_index,
            component.low_frequency, component.high_frequency, component.variant,
            component.kind, component.delay_ms, component.shape, component.polarity,
        )
        if bank_lookup is not None:
            values = bank_lookup(residual_event_name(
                component.kind, component.band_index, component.variant,
                component.delay_ms, component.shape, component.polarity,
            ))
        else:
            values = (
                decoded_residual_grain(args, bank_grain_level)
                if encoded else residual_grain(*args)
            )
        _add_panned(output[:len(values)], component.amplitude * values, component.pan)
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
