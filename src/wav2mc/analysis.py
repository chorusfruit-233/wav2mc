from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from .audio import sqrt_hann
from .config import AudioConfig, QualityProfile
from .grains import residual_grain_reference_rms


@dataclass(frozen=True)
class Component:
    frequency: int
    phase_index: int
    amplitude: float


@dataclass(frozen=True)
class ResidualComponent:
    kind: str
    band_index: int
    low_frequency: int
    high_frequency: int
    variant: int
    amplitude: float


@dataclass(frozen=True)
class AudioFrame:
    index: int
    components: tuple[Component, ...]
    residual_components: tuple[ResidualComponent, ...] = ()

    @property
    def all_components(self) -> tuple[Component | ResidualComponent, ...]:
        return self.components + self.residual_components


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


def _a_weighting_db(frequencies: np.ndarray) -> np.ndarray:
    values = np.asarray(frequencies, dtype=np.float64)
    squared = values**2
    numerator = 12194.0**2 * squared**2
    denominator = (
        (squared + 20.6**2)
        * np.sqrt((squared + 107.7**2) * (squared + 737.9**2))
        * (squared + 12194.0**2)
    )
    return 20.0 * np.log10(numerator / denominator) + 2.0


def _bark_scale(frequencies: np.ndarray) -> np.ndarray:
    values = np.asarray(frequencies, dtype=np.float64)
    return 13.0 * np.arctan(0.00076 * values) + 3.5 * np.arctan(
        (values / 7500.0) ** 2
    )


def _perceptual_levels_db(
    amplitudes: np.ndarray,
    frequencies: np.ndarray,
) -> np.ndarray:
    amplitude_db = 20.0 * np.log10(np.maximum(amplitudes, 1e-12))
    return amplitude_db + _a_weighting_db(frequencies)


def _audible_peak_indices(
    perceptual_levels_db: np.ndarray,
    frequencies: np.ndarray,
    peak_indices: set[int],
    masking_offset_db: float,
) -> set[int]:
    """Apply an asymmetric Bark-domain spreading model to tonal peaks."""
    bark_positions = _bark_scale(frequencies)
    audible: list[int] = []
    ordered = sorted(
        peak_indices,
        key=lambda index: float(perceptual_levels_db[index]),
        reverse=True,
    )
    for index in ordered:
        level = float(perceptual_levels_db[index])
        masked = False
        for masker in audible:
            bark_delta = float(bark_positions[index] - bark_positions[masker])
            slope = 12.0 if bark_delta > 0.0 else 27.0
            masking_threshold = (
                float(perceptual_levels_db[masker])
                - masking_offset_db
                - slope * abs(bark_delta)
            )
            if level <= masking_threshold:
                masked = True
                break
        if not masked:
            audible.append(index)
    return set(audible)


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


def _residual_band_masks(
    frequencies: np.ndarray,
    config: AudioConfig,
) -> list[tuple[int, int, int, np.ndarray]]:
    return [
        (
            band_index,
            low,
            high,
            np.flatnonzero((frequencies >= low) & (frequencies < high)),
        )
        for band_index, low, high in config.residual_bands
    ]


def _transient_components_by_frame(
    audio: np.ndarray,
    config: AudioConfig,
    quality: QualityProfile,
    frame_count: int,
) -> dict[int, tuple[ResidualComponent, ...]]:
    if (
        not config.hybrid_residual
        or quality.max_transient_components <= 0
        or not config.residual_bands
    ):
        return {}

    short_size = max(256, round(config.sample_rate * 0.025))
    short_hop = max(1, round(config.sample_rate * 0.010))
    fft_size = 1 << (short_size - 1).bit_length()
    window = np.hanning(short_size)
    frequencies = np.fft.rfftfreq(fft_size, 1.0 / config.sample_rate)
    band_masks = _residual_band_masks(frequencies, config)
    band_weights = np.asarray(
        [
            10.0
            ** (
                float(
                    _a_weighting_db(
                        np.asarray([(low + high) / 2.0], dtype=np.float64)
                    )[0]
                )
                / 20.0
            )
            for _, low, high, _ in band_masks
        ],
        dtype=np.float64,
    )

    short_frame_count = max(
        1,
        int(np.ceil(max(0, audio.size - short_size) / short_hop)) + 1,
    )
    novelty = np.zeros(short_frame_count, dtype=np.float64)
    band_increases = np.zeros(
        (short_frame_count, len(band_masks)),
        dtype=np.float32,
    )
    band_levels = np.zeros_like(band_increases)
    previous_magnitude = np.zeros(frequencies.size, dtype=np.float64)

    for short_index in range(short_frame_count):
        start = short_index * short_hop
        chunk = audio[start : start + short_size]
        if chunk.size < short_size:
            chunk = np.pad(chunk, (0, short_size - chunk.size))
        magnitude = np.abs(np.fft.rfft(chunk * window, n=fft_size))
        if short_index == 0:
            previous_magnitude = magnitude
            continue

        increase = np.maximum(magnitude - previous_magnitude, 0.0)
        current_levels = np.zeros(len(band_masks), dtype=np.float64)
        for band_position, (_, _, _, indices) in enumerate(band_masks):
            if not indices.size:
                continue
            delta_rms = float(
                np.sqrt(2.0 * np.sum(increase[indices] ** 2)) / short_size
            )
            current_rms = float(
                np.sqrt(2.0 * np.sum(magnitude[indices] ** 2)) / short_size
            )
            band_increases[short_index, band_position] = delta_rms
            band_levels[short_index, band_position] = current_rms
            current_levels[band_position] = current_rms
        weighted_increase = float(
            np.dot(band_increases[short_index], band_weights)
        )
        weighted_level = float(np.dot(current_levels, band_weights))
        novelty[short_index] = weighted_increase / max(weighted_level, 1e-12)
        previous_magnitude = magnitude

    usable_novelty = novelty[1:]
    median = float(np.median(usable_novelty))
    mad = float(np.median(np.abs(usable_novelty - median)))
    robust_deviation = 1.4826 * mad
    high_threshold = max(0.09, median + 3.0 * robust_deviation)
    low_threshold = max(
        0.045,
        median + robust_deviation,
        high_threshold * 0.4,
    )
    components_by_frame: dict[int, dict[int, ResidualComponent]] = {}
    armed = True
    last_trigger = -100

    for short_index in range(1, short_frame_count):
        value = float(novelty[short_index])
        if not armed:
            if value <= low_threshold or short_index - last_trigger >= 10:
                armed = True
            else:
                continue
        if short_index - last_trigger < 5 or value < high_threshold:
            continue
        local_low = max(1, short_index - 2)
        local_high = min(short_frame_count, short_index + 3)
        if value < float(np.max(novelty[local_low:local_high])):
            continue

        output_index = int(
            np.rint(short_index * short_hop / config.hop_size)
        )
        if not 0 <= output_index < frame_count:
            continue
        amplitudes = band_increases[short_index]
        maximum = float(amplitudes.max(initial=0.0))
        if maximum <= 1e-10:
            continue
        floor = maximum * 10.0 ** (-24.0 / 20.0)
        previous_levels = band_levels[short_index - 1]
        current_levels = band_levels[short_index]
        candidates = [
            position
            for position, amplitude in enumerate(amplitudes)
            if float(amplitude) >= floor
            and (
                20.0
                * np.log10(
                    max(float(current_levels[position]), 1e-12)
                    / max(float(previous_levels[position]), 1e-12)
                )
                >= 2.0
                or float(amplitude)
                / max(float(current_levels[position]), 1e-12)
                >= 0.35
            )
        ]
        if not candidates:
            continue
        candidates.sort(
            key=lambda position: float(amplitudes[position] * band_weights[position]),
            reverse=True,
        )
        armed = False
        last_trigger = short_index

        frame_components = components_by_frame.setdefault(output_index, {})
        for position in candidates[: quality.max_transient_components]:
            band_index, low, high, _ = band_masks[position]
            variant = (
                short_index * 3 + band_index
            ) % config.residual_variant_count
            grain_rms = residual_grain_reference_rms(
                config.sample_rate,
                config.window_size,
                band_index,
                low,
                high,
                variant,
                "transient",
            )
            coefficient = float(amplitudes[position]) / max(
                grain_rms,
                1e-12,
            )
            component = ResidualComponent(
                kind="transient",
                band_index=band_index,
                low_frequency=low,
                high_frequency=high,
                variant=variant,
                amplitude=min(coefficient, 1.0),
            )
            previous = frame_components.get(band_index)
            if previous is None or component.amplitude > previous.amplitude:
                frame_components[band_index] = component

    return {
        frame_index: tuple(
            sorted(components.values(), key=lambda component: component.band_index)
        )
        for frame_index, components in components_by_frame.items()
    }


def _noise_residual_components(
    spectrum: np.ndarray,
    selected_fft_bins: list[int],
    config: AudioConfig,
    quality: QualityProfile,
    frame_index: int,
    reference_amplitude: float,
    excluded_bands: set[int],
    noise_tracks: dict[int, tuple[int, int]],
) -> tuple[ResidualComponent, ...]:
    if (
        not config.hybrid_residual
        or quality.max_noise_components <= 0
        or not config.residual_bands
    ):
        return ()

    frequencies = np.fft.rfftfreq(config.window_size, 1.0 / config.sample_rate)
    band_masks = _residual_band_masks(frequencies, config)
    residual_spectrum = spectrum.copy()
    for fft_bin in selected_fft_bins:
        low_bin = max(0, fft_bin - 2)
        high_bin = min(residual_spectrum.size, fft_bin + 3)
        residual_spectrum[low_bin:high_bin] = 0.0

    original_power = np.abs(spectrum) ** 2
    residual_power = np.abs(residual_spectrum) ** 2
    floor = reference_amplitude * 10.0 ** (
        quality.residual_floor_db / 20.0
    )
    candidates: list[tuple[float, int, int, int, float]] = []

    for band_index, low, high, indices in band_masks:
        if band_index in excluded_bands or not indices.size:
            continue
        band_power = residual_power[indices]
        band_rms = float(
            np.sqrt(2.0 * np.sum(band_power)) / config.window_size
        )
        tracked = band_index in noise_tracks
        effective_floor = floor * (0.5 if tracked else 1.0)
        if band_rms < effective_floor:
            continue

        source_band_power = original_power[indices]
        arithmetic_mean = float(np.mean(source_band_power))
        flatness = 0.0
        if arithmetic_mean > 1e-20:
            flatness = float(
                np.exp(np.mean(np.log(np.maximum(source_band_power, 1e-20))))
                / arithmetic_mean
            )
        if flatness < (0.05 if tracked else 0.08):
            continue
        center = (low + high) / 2.0
        perceptual_db = float(
            _a_weighting_db(np.asarray([center], dtype=np.float64))[0]
        )
        noise_likelihood = 0.2 + 0.8 * np.sqrt(np.clip(flatness, 0.0, 1.0))
        score = (
            20.0 * np.log10(max(band_rms * noise_likelihood, 1e-12))
            + perceptual_db
        )

        candidates.append(
            (score, band_index, low, high, band_rms)
        )

    candidates.sort(key=lambda item: item[0], reverse=True)
    selected = candidates[: quality.max_noise_components]
    selected_bands = {band_index for _, band_index, _, _, _ in selected}
    for band_index, (variant, missing_frames) in tuple(noise_tracks.items()):
        if band_index in selected_bands:
            continue
        missing_frames += 1
        if missing_frames > 2:
            del noise_tracks[band_index]
        else:
            noise_tracks[band_index] = (variant, missing_frames)

    components = []
    for _, band_index, low, high, band_rms in selected:
        track = noise_tracks.get(band_index)
        if track is None:
            variant = (
                frame_index * 3 + band_index
            ) % config.residual_variant_count
        else:
            variant = (track[0] + 1) % config.residual_variant_count
        noise_tracks[band_index] = (variant, 0)
        grain_rms = residual_grain_reference_rms(
            config.sample_rate,
            config.window_size,
            band_index,
            low,
            high,
            variant,
            "noise",
        )
        noise_confidence = float(np.clip(flatness, 0.0, 1.0) ** 0.25)
        coefficient = band_rms * noise_confidence / max(
            grain_rms * np.sqrt(2.0),
            1e-12,
        )
        components.append(
            ResidualComponent(
                kind="noise",
                band_index=band_index,
                low_frequency=low,
                high_frequency=high,
                variant=variant,
                amplitude=min(coefficient, 1.0),
            )
        )
    return tuple(components)


def analyse_audio(
    audio: np.ndarray,
    config: AudioConfig,
    quality: QualityProfile,
    continuity_bonus: float = 0.12,
    tracking_radius_steps: int = 1,
    tracking_hysteresis: float = 0.10,
    psychoacoustic_masking: bool = True,
) -> list[AudioFrame]:
    if continuity_bonus < 0.0:
        raise ValueError("continuity_bonus must not be negative")
    if tracking_radius_steps < 0:
        raise ValueError("tracking_radius_steps must not be negative")
    if tracking_hysteresis < 0.0:
        raise ValueError("tracking_hysteresis must not be negative")
    if quality.masking_offset_db < 0.0:
        raise ValueError("masking_offset_db must not be negative")

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
    transient_components = _transient_components_by_frame(
        audio,
        config,
        quality,
        frame_count,
    )

    frames: list[AudioFrame] = []
    previous_indices: set[int] = set()
    noise_tracks: dict[int, tuple[int, int]] = {}

    for frame_index in range(frame_count):
        start = frame_index * hop
        chunk = padded[start : start + n]
        spectrum = np.fft.rfft(chunk * window)
        bank_spectrum = spectrum[fft_bins]
        amplitudes = (2.0 * np.abs(bank_spectrum) / n).astype(np.float64)

        maximum = float(amplitudes.max(initial=0.0))
        if maximum <= 1e-10:
            frames.append(
                AudioFrame(
                    frame_index,
                    (),
                    transient_components.get(frame_index, ()),
                )
            )
            previous_indices.clear()
            noise_tracks.clear()
            continue

        floor = maximum * 10.0 ** (quality.relative_floor_db / 20.0)
        local_peaks = _track_peak_indices(
            amplitudes,
            _candidate_indices(amplitudes),
            previous_indices,
            tracking_radius_steps,
            tracking_hysteresis,
        )
        perceptual_levels = _perceptual_levels_db(amplitudes, frequencies)
        if psychoacoustic_masking:
            local_peaks = _audible_peak_indices(
                perceptual_levels,
                frequencies,
                local_peaks,
                quality.masking_offset_db,
            )
        selected: list[int] = []

        def score(index: int) -> float:
            base = float(perceptual_levels[index])
            nearest_track = min(
                (abs(index - previous) for previous in previous_indices),
                default=tracking_radius_steps + 1,
            )
            if nearest_track <= tracking_radius_steps:
                proximity = 1.0 - nearest_track / (tracking_radius_steps + 1)
                base += 20.0 * np.log10(1.0 + continuity_bonus * proximity)
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
        transients = transient_components.get(frame_index, ())
        noise_components = _noise_residual_components(
            spectrum,
            [int(fft_bins[index]) for index in selected],
            config,
            quality,
            frame_index,
            maximum,
            {component.band_index for component in transients},
            noise_tracks,
        )
        frames.append(
            AudioFrame(
                frame_index,
                tuple(components),
                noise_components + transients,
            )
        )

    return frames


def scale_frames(frames: list[AudioFrame], scale: float) -> list[AudioFrame]:
    return scale_frame_layers(frames, scale, scale)


def scale_frame_layers(
    frames: list[AudioFrame],
    tone_scale: float,
    residual_scale: float,
) -> list[AudioFrame]:
    return [
        replace(
            frame,
            components=tuple(
                replace(component, amplitude=component.amplitude * tone_scale)
                for component in frame.components
            ),
            residual_components=tuple(
                replace(
                    component,
                    amplitude=component.amplitude * residual_scale,
                )
                for component in frame.residual_components
            ),
        )
        for frame in frames
    ]


def scale_frame_residuals(
    frames: list[AudioFrame],
    tone_scale: float,
    residual_scales: dict[str, list[float]],
) -> list[AudioFrame]:
    if any(len(frames) != len(scales) for scales in residual_scales.values()):
        raise ValueError("A scale is required for every frame in each residual layer")
    return [
        replace(
            frame,
            components=tuple(
                replace(component, amplitude=component.amplitude * tone_scale)
                for component in frame.components
            ),
            residual_components=tuple(
                replace(
                    component,
                    amplitude=(
                        component.amplitude
                        * residual_scales[component.kind][index]
                    ),
                )
                for component in frame.residual_components
            ),
        )
        for index, frame in enumerate(frames)
    ]
