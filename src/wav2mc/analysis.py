from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from .audio import sqrt_hann
from .config import AudioConfig, QualityProfile


@dataclass(frozen=True)
class Component:
    frequency: int
    phase_index: int
    amplitude: float


@dataclass(frozen=True)
class AudioFrame:
    index: int
    components: tuple[Component, ...]


def _candidate_indices(amplitudes: np.ndarray) -> np.ndarray:
    if amplitudes.size < 3:
        return np.arange(amplitudes.size)
    mask = np.zeros(amplitudes.size, dtype=bool)
    mask[1:-1] = (
        (amplitudes[1:-1] >= amplitudes[:-2])
        & (amplitudes[1:-1] >= amplitudes[2:])
    )
    mask[0] = amplitudes[0] >= amplitudes[1]
    mask[-1] = amplitudes[-1] >= amplitudes[-2]
    return np.flatnonzero(mask)


def _track_peak_indices(
    amplitudes: np.ndarray,
    peak_indices: np.ndarray,
    previous_indices: set[int],
    tracking_radius_steps: int,
    tracking_hysteresis: float,
) -> set[int]:
    """Associate nearby peaks and retain a track until a new bin is stronger."""
    tracked_indices = {int(index) for index in peak_indices}
    if tracking_radius_steps == 0 or not previous_indices or not tracked_indices:
        return tracked_indices

    associations: list[tuple[int, float, int, int]] = []
    for previous_index in previous_indices:
        for peak_index in tracked_indices:
            distance = abs(peak_index - previous_index)
            if distance <= tracking_radius_steps:
                associations.append(
                    (
                        distance,
                        -float(amplitudes[previous_index]),
                        previous_index,
                        peak_index,
                    )
                )

    claimed_previous: set[int] = set()
    claimed_peaks: set[int] = set()
    for _, _, previous_index, peak_index in sorted(associations):
        if previous_index in claimed_previous or peak_index in claimed_peaks:
            continue
        claimed_previous.add(previous_index)
        claimed_peaks.add(peak_index)

        if peak_index == previous_index:
            continue
        switch_level = float(amplitudes[previous_index]) * (
            1.0 + tracking_hysteresis
        )
        if float(amplitudes[peak_index]) <= switch_level:
            tracked_indices.remove(peak_index)
            tracked_indices.add(previous_index)

    return tracked_indices


def analyse_audio(
    audio: np.ndarray,
    config: AudioConfig,
    quality: QualityProfile,
    continuity_bonus: float = 0.12,
    tracking_radius_steps: int = 1,
    tracking_hysteresis: float = 0.10,
) -> list[AudioFrame]:
    if continuity_bonus < 0.0:
        raise ValueError("continuity_bonus must not be negative")
    if tracking_radius_steps < 0:
        raise ValueError("tracking_radius_steps must not be negative")
    if tracking_hysteresis < 0.0:
        raise ValueError("tracking_hysteresis must not be negative")

    n = config.window_size
    hop = config.hop_size
    if hop * 2 != n:
        raise ValueError("This base project expects a 50% overlap: hop_size * 2 == window_size")

    window = sqrt_hann(n)
    frequencies = np.asarray(config.frequencies, dtype=np.int32)
    fft_bins = np.rint(frequencies * n / config.sample_rate).astype(np.int32)
    max_fft_bin = n // 2
    if np.any(fft_bins > max_fft_bin):
        raise ValueError("Frequency bank exceeds the Nyquist frequency")

    frame_count = max(1, int(np.ceil(max(0, audio.size - n) / hop)) + 1)
    padded_size = (frame_count - 1) * hop + n
    padded = np.pad(audio, (0, max(0, padded_size - audio.size)))

    frames: list[AudioFrame] = []
    previous_indices: set[int] = set()

    for frame_index in range(frame_count):
        start = frame_index * hop
        chunk = padded[start : start + n]
        spectrum = np.fft.rfft(chunk * window)
        bank_spectrum = spectrum[fft_bins]
        amplitudes = (2.0 * np.abs(bank_spectrum) / n).astype(np.float64)

        maximum = float(amplitudes.max(initial=0.0))
        if maximum <= 1e-10:
            frames.append(AudioFrame(frame_index, ()))
            previous_indices.clear()
            continue

        floor = maximum * 10.0 ** (quality.relative_floor_db / 20.0)
        local_peaks = _track_peak_indices(
            amplitudes,
            _candidate_indices(amplitudes),
            previous_indices,
            tracking_radius_steps,
            tracking_hysteresis,
        )
        selected: list[int] = []

        def score(index: int) -> float:
            base = float(amplitudes[index])
            nearest_track = min(
                (abs(index - previous) for previous in previous_indices),
                default=tracking_radius_steps + 1,
            )
            if nearest_track <= tracking_radius_steps:
                proximity = 1.0 - nearest_track / (tracking_radius_steps + 1)
                base *= 1.0 + continuity_bonus * proximity
            return base

        for low, high, count in quality.band_limits:
            band = [
                i
                for i, frequency in enumerate(frequencies)
                if low <= int(frequency) < high
                and amplitudes[i] >= floor
                and i in local_peaks
            ]
            band.sort(key=score, reverse=True)
            selected.extend(band[:count])

        selected = list(dict.fromkeys(selected))
        if len(selected) < quality.max_components:
            remaining = [
                i
                for i in range(frequencies.size)
                if i not in selected
                and amplitudes[i] >= floor
                and i in local_peaks
            ]
            remaining.sort(key=score, reverse=True)
            selected.extend(remaining[: quality.max_components - len(selected)])

        selected = selected[: quality.max_components]
        selected.sort(key=lambda i: int(frequencies[i]))

        components: list[Component] = []
        for index in selected:
            phase = float(np.angle(bank_spectrum[index])) % (2.0 * np.pi)
            phase_index = int(
                np.rint(phase / (2.0 * np.pi) * config.phase_count)
            ) % config.phase_count
            components.append(
                Component(
                    frequency=int(frequencies[index]),
                    phase_index=phase_index,
                    amplitude=float(amplitudes[index]),
                )
            )

        previous_indices = set(selected)
        frames.append(AudioFrame(frame_index, tuple(components)))

    return frames


def scale_frames(frames: list[AudioFrame], scale: float) -> list[AudioFrame]:
    return [
        replace(
            frame,
            components=tuple(
                replace(component, amplitude=component.amplitude * scale)
                for component in frame.components
            ),
        )
        for frame in frames
    ]
