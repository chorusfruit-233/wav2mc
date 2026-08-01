import numpy as np

from wav2mc.analysis import (
    _audible_peak_indices,
    _perceptual_levels_db,
    analyse_audio,
)
from wav2mc.config import AudioConfig, QUALITY_PROFILES


def test_a_weighting_prioritizes_audible_midrange() -> None:
    frequencies = np.asarray([80, 1000], dtype=np.int32)
    amplitudes = np.asarray([0.1, 0.1], dtype=np.float64)

    levels = _perceptual_levels_db(amplitudes, frequencies)

    assert levels[1] > levels[0] + 20.0


def test_bark_masking_removes_only_nearby_weak_peak() -> None:
    frequencies = np.asarray([440, 460, 2000], dtype=np.int32)
    amplitudes = np.asarray([0.8, 0.05, 0.05], dtype=np.float64)
    levels = _perceptual_levels_db(amplitudes, frequencies)

    audible = _audible_peak_indices(
        levels,
        frequencies,
        peak_indices={0, 1, 2},
        masking_offset_db=10.0,
    )

    assert audible == {0, 2}


def test_analysis_masks_nearby_tone_but_keeps_distant_tone() -> None:
    config = AudioConfig(max_frequency=2500)
    time = np.arange(config.sample_rate * 2) / config.sample_rate
    audio = (
        0.8 * np.cos(2 * np.pi * 440 * time)
        + 0.1 * np.cos(2 * np.pi * 500 * time + 0.2)
        + 0.1 * np.cos(2 * np.pi * 2000 * time + 0.4)
    ).astype(np.float32)

    masked = analyse_audio(
        audio,
        config,
        QUALITY_PROFILES["normal"],
        psychoacoustic_masking=True,
    )
    unmasked = analyse_audio(
        audio,
        config,
        QUALITY_PROFILES["normal"],
        psychoacoustic_masking=False,
    )

    assert {component.frequency for component in masked[0].components} == {
        440,
        2000,
    }
    assert {component.frequency for component in unmasked[0].components} == {
        440,
        500,
        2000,
    }
