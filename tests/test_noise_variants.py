from dataclasses import replace

import numpy as np
import pytest

from wav2mc.analysis import _noise_variant, analyse_audio
from wav2mc.config import AudioConfig, QUALITY_PROFILES
from wav2mc.preview import synthesize_preview


@pytest.mark.parametrize("variant_count", [1, 2, 4])
def test_noise_variant_selection_is_bounded_and_reproducible(variant_count: int) -> None:
    def sequence(band: int) -> list[int]:
        result = []
        previous = None
        for frame in range(200):
            previous = _noise_variant(frame, band, variant_count, previous)
            result.append(previous)
        return result

    variants = sequence(9)
    assert variants == sequence(9)
    assert all(0 <= variant < variant_count for variant in variants)
    if variant_count > 1:
        assert all(a != b for a, b in zip(variants, variants[1:]))
    if variant_count > 2:
        assert variants != sequence(10)
        assert np.mean(np.asarray(variants[4:]) == variants[:-4]) < 0.5


def test_continuous_noise_does_not_repeat_every_200ms() -> None:
    config = AudioConfig(min_frequency=5000, max_frequency=6299)
    quality = replace(
        QUALITY_PROFILES["normal"], max_components=0,
        max_noise_components=1, max_transient_components=0,
    )
    rng = np.random.default_rng(42)
    spectrum = np.fft.rfft(rng.standard_normal(config.sample_rate * 3))
    frequencies = np.fft.rfftfreq(config.sample_rate * 3, 1 / config.sample_rate)
    spectrum[(frequencies < 5000) | (frequencies >= 6300)] = 0
    audio = np.fft.irfft(spectrum).astype(np.float32)
    frames = analyse_audio(audio, config, quality)
    preview = synthesize_preview(frames, config)[config.window_size:-config.window_size]
    lag = 4 * config.hop_size
    correlation = np.corrcoef(preview[:-lag], preview[lag:])[0, 1]

    assert any(frame.residual_components for frame in frames)
    assert np.isfinite(correlation)
    assert abs(correlation) < 0.55
    np.testing.assert_array_equal(
        synthesize_preview(analyse_audio(audio, config, quality), config),
        synthesize_preview(frames, config),
    )
