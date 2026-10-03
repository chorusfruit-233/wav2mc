import numpy as np
import pytest

from wav2mc.analysis import analyse_audio
from wav2mc.config import AudioConfig, QUALITY_PROFILES
from wav2mc.pipeline import _analyse_channels, _residual_frame_scales
from wav2mc.preview import synthesize_preview


@pytest.mark.parametrize("onset_ms", [0, 5, 20, 250, 920, 960, 980])
def test_attacks_at_recording_boundaries_are_preserved(onset_ms: int) -> None:
    config = AudioConfig(min_frequency=80, max_frequency=12000)
    source = np.zeros(config.sample_rate, dtype=np.float32)
    start = round(config.sample_rate * onset_ms / 1000)
    source[start:start + 64] = np.hanning(128)[:64]
    frames = analyse_audio(source, config, QUALITY_PROFILES["normal"])
    attacks = [f for f in frames if any(c.kind == "transient" for c in f.residual_components)]

    assert len(attacks) == 1
    assert all(
        abs(attacks[0].index * config.hop_ms + c.delay_ms - onset_ms) <= 5
        for c in attacks[0].residual_components if c.kind == "transient"
    )
    assert np.all(np.isfinite(synthesize_preview(frames, config)))
    if onset_ms >= 960:
        assert len(frames) > 19
        assert not frames[-1].components


@pytest.mark.parametrize("sample_count", [64, 400, 1200])
def test_short_opening_attack_is_preserved(sample_count: int) -> None:
    config = AudioConfig(min_frequency=80, max_frequency=12000)
    audio = np.zeros(sample_count, dtype=np.float32)
    audio[:16] = np.hanning(32)[:16]
    frames = analyse_audio(audio, config, QUALITY_PROFILES["normal"])

    assert any(c.kind == "transient" for c in frames[0].residual_components)


def test_late_transient_in_one_stereo_channel_keeps_tail() -> None:
    config = AudioConfig(min_frequency=80, max_frequency=12000)
    audio = np.zeros((config.sample_rate, 2), dtype=np.float32)
    start = round(0.98 * config.sample_rate)
    audio[start:start + 64, 1] = np.hanning(128)[:64]
    frames = _analyse_channels(audio, config, QUALITY_PROFILES["normal"], True)
    preview = synthesize_preview(frames, config, stereo=True)
    scales = _residual_frame_scales(
        frames, config, np.zeros_like(preview), 1.0, 1.0, 1.0,
    )

    assert len(frames) == 20
    assert all(c.pan == 1 for c in frames[-1].residual_components)
    assert not np.any(preview[:, 0])
    assert np.any(preview[config.sample_rate:, 1])
    assert all(len(values) == len(frames) for values in scales.values())
