from __future__ import annotations

from functools import lru_cache

import numpy as np

from .analysis import AudioFrame
from .audio import tonal_window
from .config import AudioConfig
from .grains import residual_grain
from .utils import (
    CancelCheck,
    ProgressCallback,
    check_cancelled,
    emit_progress,
)


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


def synthesize_preview(
    frames: list[AudioFrame],
    config: AudioConfig,
    stereo: bool | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> np.ndarray:
    n = config.window_size
    hop = config.hop_size
    window = tonal_window(n)
    @lru_cache(maxsize=128)
    def grain(frequency: int, phase_index: int, pitch: float) -> np.ndarray:
        # Ideal resampling of the bank grain: pitch changes both the carrier
        # and the envelope duration. Vorbis and the game's resampler add error.
        positions = np.arange(int(np.ceil(n / pitch)), dtype=np.float64) * pitch
        if pitch == 1.0:
            envelope = window
        else:
            envelope = 0.5 - 0.5 * np.cos(2.0 * np.pi * positions / (n - 1))
            envelope[positions > n - 1] = 0.0
        phase = 2.0 * np.pi * phase_index / config.phase_count
        values = envelope * np.cos(
            2.0 * np.pi * frequency * positions / config.sample_rate + phase
        )
        return values.astype(np.float32)

    output_size = max(
        [n] + [
            frame.index * hop + max(
                n,
                max((int(np.ceil(n / c.pitch)) for c in frame.components), default=n),
            )
            for frame in frames
        ]
    )
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
        end = start + n
        for component in frame.components:
            values = grain(component.frequency, component.phase_index, component.pitch)
            _add_panned(
                output[start:start + values.size],
                component.amplitude * values,
                component.pan,
            )
        output[start:end] += synthesize_residual_frame(
            frame,
            config,
            stereo=stereo,
        )

    check_cancelled(cancel_check)
    emit_progress(progress_callback, "reconstruct", 1.0, "Preview reconstructed")
    return output


def synthesize_residual_frame(
    frame: AudioFrame,
    config: AudioConfig,
    kind: str | None = None,
    stereo: bool | None = None,
) -> np.ndarray:
    if stereo is None:
        stereo = any(component.pan != 0.0 for component in frame.all_components)
    output_shape = (config.window_size, 2) if stereo else config.window_size
    output = np.zeros(output_shape, dtype=np.float32)
    for component in frame.residual_components:
        if kind is not None and component.kind != kind:
            continue
        _add_panned(
            output,
            component.amplitude
            * residual_grain(
                config.sample_rate,
                config.window_size,
                component.band_index,
                component.low_frequency,
                component.high_frequency,
                component.variant,
                component.kind,
            ),
            component.pan,
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
