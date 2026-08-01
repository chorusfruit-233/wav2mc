import numpy as np

from wav2mc.analysis import analyse_audio
from wav2mc.cli import _audio_config, build_parser
from wav2mc.config import AudioConfig, QUALITY_PROFILES


def test_adaptive_frequency_grid_uses_log_like_spacing() -> None:
    frequencies = AudioConfig(min_frequency=80, max_frequency=20000).frequencies

    assert len(frequencies) == 222
    assert frequencies[:4] == (80, 100, 120, 140)
    assert frequencies[-4:] == (19040, 19360, 19680, 20000)
    assert 980 in frequencies and 1000 in frequencies and 1040 in frequencies
    assert 3960 in frequencies and 4000 in frequencies and 4080 in frequencies
    assert 7920 in frequencies and 8000 in frequencies and 8160 in frequencies
    assert 11840 in frequencies and 12000 in frequencies and 12320 in frequencies


def test_explicit_frequency_step_selects_uniform_grid() -> None:
    parser = build_parser()
    adaptive = _audio_config(parser.parse_args(["bank-build"]))
    uniform = _audio_config(
        parser.parse_args(["bank-build", "--frequency-step", "40"])
    )

    assert adaptive.frequency_grid == "adaptive"
    assert uniform.frequency_grid == "uniform"
    assert uniform.frequencies[:4] == (20, 60, 100, 140)


def test_quality_profiles_enforce_fixed_band_budgets() -> None:
    config = AudioConfig(min_frequency=60, max_frequency=12000)
    quality = QUALITY_PROFILES["normal"]
    rng = np.random.default_rng(7)
    audio = rng.normal(0.0, 0.2, config.window_size).astype(np.float32)

    frame = analyse_audio(
        audio,
        config,
        quality,
        psychoacoustic_masking=False,
    )[0]

    assert len(frame.components) <= 20
    for low, high, budget in quality.band_limits:
        count = sum(low <= component.frequency < high for component in frame.components)
        assert count <= budget


def test_recommended_quality_profile_limits() -> None:
    assert QUALITY_PROFILES["voice"].max_components == 12
    assert QUALITY_PROFILES["normal"].max_components == 20
    assert QUALITY_PROFILES["high"].max_components == 24
    assert QUALITY_PROFILES["experimental"].max_components == 32
