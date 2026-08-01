from pathlib import Path

import numpy as np

from wav2mc.analysis import analyse_audio
from wav2mc.audio import sqrt_hann
from wav2mc.config import AudioConfig, QUALITY_PROFILES, QualityProfile
from wav2mc.datapack import build_data_pack


def test_window_endpoints_are_zero() -> None:
    window = sqrt_hann(4800)
    assert window[0] == 0.0
    assert window[-1] == 0.0


def test_detects_440_hz() -> None:
    config = AudioConfig(max_frequency=1000)
    time = np.arange(config.window_size * 2) / config.sample_rate
    audio = (0.8 * np.cos(2 * np.pi * 440 * time + 0.3)).astype(np.float32)
    frames = analyse_audio(audio, config, QUALITY_PROFILES["normal"])
    assert frames
    assert any(component.frequency == 440 for component in frames[0].components)


def test_peak_tracking_reduces_neighbor_jitter() -> None:
    config = AudioConfig(max_frequency=1000)
    quality = QualityProfile("tracking", 1, ((80, 1001, 1),), -50.0)
    frame_count = 60
    sample_count = config.hop_size * (frame_count - 1) + config.window_size
    time = np.arange(sample_count) / config.sample_rate
    noise = np.random.default_rng(42).normal(size=sample_count)
    audio = (
        0.5 * np.cos(2 * np.pi * 450 * time + 0.31) + 0.01 * noise
    ).astype(np.float32)

    untracked = analyse_audio(
        audio,
        config,
        quality,
        continuity_bonus=0.0,
        tracking_radius_steps=0,
    )
    tracked = analyse_audio(audio, config, quality, continuity_bonus=0.0)
    untracked_frequencies = [frame.components[0].frequency for frame in untracked]
    tracked_frequencies = [frame.components[0].frequency for frame in tracked]
    untracked_changes = sum(
        current != previous
        for previous, current in zip(
            untracked_frequencies,
            untracked_frequencies[1:],
        )
    )
    tracked_changes = sum(
        current != previous
        for previous, current in zip(
            tracked_frequencies,
            tracked_frequencies[1:],
        )
    )

    assert untracked_changes > 10
    assert tracked_changes < untracked_changes / 4


def test_peak_tracking_follows_gradual_frequency_changes() -> None:
    config = AudioConfig(max_frequency=1000)
    quality = QualityProfile("tracking", 1, ((80, 1001, 1),), -50.0)
    frame_count = 60
    sample_count = config.hop_size * (frame_count - 1) + config.window_size
    instantaneous_frequency = np.linspace(430.0, 490.0, sample_count)
    phase = 2 * np.pi * np.cumsum(instantaneous_frequency) / config.sample_rate
    audio = (0.8 * np.cos(phase)).astype(np.float32)

    frames = analyse_audio(audio, config, quality)
    frequencies = [frame.components[0].frequency for frame in frames]

    assert frequencies[0] == 440
    assert frequencies[-1] == 480
    assert set(frequencies) == {440, 460, 480}
    assert all(
        current - previous in (0, config.frequency_step)
        for previous, current in zip(frequencies, frequencies[1:])
    )


def test_silence_resets_peak_tracking() -> None:
    config = AudioConfig(max_frequency=1000)
    quality = QualityProfile("tracking", 1, ((80, 1001, 1),), -50.0)
    segment_size = config.window_size * 4
    time = np.arange(segment_size) / config.sample_rate
    first_tone = (0.5 * np.cos(2 * np.pi * 449 * time + 0.31)).astype(
        np.float32
    )
    second_tone = (0.5 * np.cos(2 * np.pi * 451 * time + 0.31)).astype(
        np.float32
    )
    silence = np.zeros(config.window_size * 2, dtype=np.float32)

    frames = analyse_audio(
        np.concatenate((first_tone, silence, second_tone)),
        config,
        quality,
        continuity_bonus=0.0,
    )
    frequencies = [
        frame.components[0].frequency if frame.components else None
        for frame in frames
    ]

    first_silent_frame = frequencies.index(None)
    first_resumed_frequency = next(
        frequency
        for frequency in frequencies[first_silent_frame:]
        if frequency is not None
    )
    assert set(frequencies[:first_silent_frame]) == {440}
    assert first_resumed_frequency == 460


def test_builds_data_pack(tmp_path: Path) -> None:
    config = AudioConfig(max_frequency=1000)
    time = np.arange(config.window_size) / config.sample_rate
    audio = (0.8 * np.cos(2 * np.pi * 440 * time)).astype(np.float32)
    frames = analyse_audio(audio, config, QUALITY_PROFILES["low"])
    target = tmp_path / "song.zip"
    build_data_pack(
        target,
        frames,
        namespace="test_song",
        bank_namespace="wav2mc",
        pack_format=81,
        layout="modern",
    )
    assert target.is_file()
    assert target.stat().st_size > 0
