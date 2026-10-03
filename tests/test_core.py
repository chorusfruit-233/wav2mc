import io
import json
import zipfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from wav2mc.analysis import AudioFrame, Component, analyse_audio
from wav2mc.audio import sqrt_hann, tonal_window
from wav2mc.bank import build_resource_pack
from wav2mc.config import (
    DEFAULT_DATA_PACK_FORMAT,
    DEFAULT_RESOURCE_PACK_FORMAT,
    AudioConfig,
    QUALITY_PROFILES,
    QualityProfile,
)
from wav2mc.datapack import build_data_pack
from wav2mc.preview import synthesize_preview


def test_window_endpoints_are_zero() -> None:
    window = sqrt_hann(4800)
    assert window[0] == 0.0
    assert window[-1] == 0.0


def test_tonal_window_preserves_gain_across_overlapping_grains() -> None:
    window = tonal_window(4800)
    assert window[0] == window[-1] == 0.0
    np.testing.assert_allclose(window[:2400] + window[2400:], 1.0, atol=0.0004)


@pytest.mark.parametrize("frequency", [80, 440, 1040, 4400])
@pytest.mark.parametrize("phase_index", [0, 3, 7])
def test_tonal_reconstruction_preserves_level_and_waveform(
    frequency: int, phase_index: int,
) -> None:
    config = AudioConfig(min_frequency=60, max_frequency=8000, hybrid_residual=False)
    time = np.arange(config.sample_rate) / config.sample_rate
    phase = 2.0 * np.pi * phase_index / config.phase_count
    audio = (0.6 * np.cos(2.0 * np.pi * frequency * time + phase)).astype(np.float32)

    frames = analyse_audio(audio, config, QUALITY_PROFILES["normal"])
    preview = synthesize_preview(frames, config)
    # The first and last grain intentionally fade in/out; measure steady overlap.
    reference = audio[config.window_size:-config.window_size]
    actual = preview[config.window_size:audio.size - config.window_size]
    relative_error = np.linalg.norm(reference - actual) / np.linalg.norm(reference)

    assert relative_error < 0.003
    np.testing.assert_allclose(
        [frame.components[0].amplitude for frame in frames], 0.6, atol=0.002,
    )


def test_tonal_chord_preserves_relative_levels() -> None:
    config = AudioConfig(min_frequency=80, max_frequency=2000, hybrid_residual=False)
    time = np.arange(config.sample_rate) / config.sample_rate
    audio = sum(
        amplitude * np.cos(2.0 * np.pi * frequency * time)
        for frequency, amplitude in [(440, 0.4), (660, 0.25), (880, 0.15)]
    ).astype(np.float32)

    frames = analyse_audio(audio, config, QUALITY_PROFILES["normal"])
    preview = synthesize_preview(frames, config)
    reference = audio[config.window_size:-config.window_size]
    actual = preview[config.window_size:audio.size - config.window_size]

    assert np.linalg.norm(reference - actual) / np.linalg.norm(reference) < 0.003


def test_resource_pack_tone_matches_preview_envelope(tmp_path: Path) -> None:
    config = AudioConfig(
        min_frequency=440, max_frequency=440, phase_count=4, hybrid_residual=False,
    )
    target = tmp_path / "bank.zip"
    build_resource_pack(target, config, DEFAULT_RESOURCE_PACK_FORMAT, grain_level=0.6)
    frame = AudioFrame(0, (Component(440, 1, 0.6),))
    expected = synthesize_preview([frame], config)

    with zipfile.ZipFile(target) as archive:
        metadata = json.loads(archive.read("wav2mc-bank.json"))
        encoded = archive.read("assets/wav2mc/sounds/grain/f0440/p01.ogg")
    actual, sample_rate = sf.read(io.BytesIO(encoded), dtype="float32")

    assert metadata["tonal_window"] == "hann"
    assert sample_rate == config.sample_rate
    assert actual.shape == expected.shape
    # Allow Vorbis coding error, while detecting an incompatible grain window.
    assert np.linalg.norm(actual - expected) / np.linalg.norm(expected) < 0.04


def test_stereo_preview_routes_components_to_separate_channels() -> None:
    config = AudioConfig(max_frequency=1000)
    frame = AudioFrame(
        index=0,
        components=(
            Component(440, 0, 0.5, pan=-1.0),
            Component(660, 0, 0.5, pan=1.0),
        ),
    )

    preview = synthesize_preview([frame], config)

    assert preview.shape == (config.window_size, 2)
    frequencies = np.fft.rfftfreq(config.window_size, 1.0 / config.sample_rate)
    left_peak = frequencies[int(np.argmax(np.abs(np.fft.rfft(preview[:, 0]))))]
    right_peak = frequencies[int(np.argmax(np.abs(np.fft.rfft(preview[:, 1]))))]
    assert left_peak == 440.0
    assert right_peak == 660.0


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
        pack_format=DEFAULT_DATA_PACK_FORMAT,
        layout="modern",
    )
    assert target.is_file()
    assert target.stat().st_size > 0
