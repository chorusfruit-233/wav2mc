import io
import zipfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from wav2mc.analysis import AudioFrame, Component, analyse_audio
from wav2mc.bank import build_resource_pack
from wav2mc.config import (
    AudioConfig,
    DEFAULT_DATA_PACK_FORMAT,
    DEFAULT_RESOURCE_PACK_FORMAT,
    DEVICE_PROFILES,
    QUALITY_PROFILES,
    device_audio_config,
)
from wav2mc.datapack import build_data_pack
from wav2mc.preview import synthesize_preview


@pytest.mark.parametrize("frequency", [80, 443, 445, 450, 1050, 4420, 4440])
def test_between_bank_frequencies_preserve_pitch_and_level(frequency: int) -> None:
    config = device_audio_config(
        AudioConfig(hybrid_residual=False), DEVICE_PROFILES["normal"],
    )
    time = np.arange(config.sample_rate) / config.sample_rate
    reference = (0.6 * np.cos(2 * np.pi * frequency * time)).astype(np.float32)
    frames = analyse_audio(reference, config, QUALITY_PROFILES["normal"])
    preview = synthesize_preview(frames, config)
    crop = slice(config.window_size, reference.size - config.window_size)

    components = [c for frame in frames for c in frame.components]
    assert len(components) == len(frames)
    assert all(c.frequency in config.frequencies for c in components)
    np.testing.assert_allclose(
        [c.playback_frequency for c in components], frequency, atol=0.03,
    )
    reference_level = np.linalg.norm(reference[crop])
    assert np.linalg.norm(preview[crop]) / reference_level == pytest.approx(
        1.0, abs=0.02,
    )
    # Includes a non-quantized phase progression, not just aligned pure tones.
    assert np.linalg.norm(preview[crop] - reference[crop]) / reference_level < 0.17


def test_frequency_estimation_follows_a_sweep() -> None:
    config = AudioConfig(min_frequency=80, max_frequency=1000, hybrid_residual=False)
    frequency = np.linspace(430, 490, config.sample_rate * 2)
    phase = 2 * np.pi * np.cumsum(frequency) / config.sample_rate
    frames = analyse_audio(
        (0.6 * np.cos(phase)).astype(np.float32), config, QUALITY_PROFILES["normal"],
    )
    estimated = np.asarray([
        max(frame.components, key=lambda c: c.amplitude).playback_frequency
        for frame in frames
    ])
    centers = [frame.index * config.hop_size + config.window_size // 2 for frame in frames]

    np.testing.assert_allclose(estimated, frequency[centers], atol=0.1)
    assert np.all(np.diff(estimated) > 0)


@pytest.mark.parametrize("frequency", [20, 40, 60, 80])
@pytest.mark.parametrize("phase", [0.0, 0.5, 1.5])
def test_bass_peak_at_frequency_boundary_is_not_dropped(
    frequency: int, phase: float,
) -> None:
    config = AudioConfig(
        min_frequency=frequency, max_frequency=1000, hybrid_residual=False,
    )
    time = np.arange(config.sample_rate // 2) / config.sample_rate
    audio = (0.6 * np.cos(2 * np.pi * frequency * time + phase)).astype(np.float32)
    frames = analyse_audio(audio, config, QUALITY_PROFILES["experimental"])

    assert all(len(frame.components) == 1 for frame in frames)
    np.testing.assert_allclose(
        [frame.components[0].playback_frequency for frame in frames],
        frequency, atol=0.15,
    )


@pytest.mark.parametrize("pitch", [0.5, 0.9, 1.0, 1.1, 2.0])
def test_preview_changes_grain_duration_with_pitch(pitch: float) -> None:
    config = AudioConfig()
    frame = AudioFrame(2, (Component(440, 0, 0.6, pan=-1, pitch=pitch),))
    preview = synthesize_preview([frame], config)
    start = frame.index * config.hop_size
    size = int(np.ceil(config.window_size / pitch))

    assert len(preview) == start + max(size, config.window_size)
    assert not np.any(preview[:start])
    assert not np.any(preview[:, 1])
    assert not np.any(preview[start + size:])
    assert np.max(np.abs(preview[start:start + size, 0])) > 0.55
    if pitch < 1:
        assert np.any(preview[start + config.window_size:, 0])


@pytest.mark.parametrize("pitch", [0, 0.49, 2.01, float("nan"), float("inf")])
def test_pitch_outside_playback_range_is_rejected(pitch: float) -> None:
    with pytest.raises(ValueError, match="pitch"):
        Component(440, 0, 0.6, pitch=pitch)


def test_datapack_and_preview_use_the_same_pitch(tmp_path: Path) -> None:
    config = AudioConfig(
        min_frequency=440, max_frequency=440, phase_count=4, hybrid_residual=False,
    )
    component = Component(440, 1, 0.6, pitch=1.023456789)
    frame = AudioFrame(0, (component,))
    pack = tmp_path / "song.zip"
    bank = tmp_path / "bank.zip"
    build_data_pack(pack, [frame], "pitch_test", "wav2mc", DEFAULT_DATA_PACK_FORMAT, "modern")
    build_resource_pack(bank, config, DEFAULT_RESOURCE_PACK_FORMAT)
    with zipfile.ZipFile(pack) as archive:
        command = archive.read("data/pitch_test/function/frame/000000.mcfunction").decode()
    pitch = float(command.split()[-2])
    assert pitch == component.pitch
    assert "grain.f0440.p01" in command
    with zipfile.ZipFile(bank) as archive:
        grain, _ = sf.read(io.BytesIO(
            archive.read("assets/wav2mc/sounds/grain/f0440/p01.ogg")
        ))
    positions = np.arange(int(np.ceil(config.window_size / pitch))) * pitch
    decoded = component.amplitude * np.interp(positions, np.arange(grain.size), grain, right=0)
    preview = synthesize_preview([frame], config)[:decoded.size]
    # Compare against the actual exported Vorbis grain at the command's speed.
    assert np.linalg.norm(decoded - preview) / np.linalg.norm(preview) < 0.04
