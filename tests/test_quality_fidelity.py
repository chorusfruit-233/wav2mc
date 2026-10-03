from dataclasses import replace
from pathlib import Path
import json
import zipfile

import numpy as np
import pytest

from wav2mc.analysis import AudioFrame, Component, ResidualComponent, analyse_audio
from wav2mc.bank import build_resource_pack
from wav2mc.config import AudioConfig, DEFAULT_DATA_PACK_FORMAT, DEFAULT_RESOURCE_PACK_FORMAT, QUALITY_PROFILES
from wav2mc.datapack import build_data_pack
from wav2mc.grains import codec_round_trip, residual_grain, tonal_grain
from wav2mc.pipeline import _analyse_channels, _residual_frame_scales
from wav2mc.preview import open_preview_bank, synthesize_preview


def snr(reference: np.ndarray, actual: np.ndarray) -> float:
    return float(10 * np.log10(np.mean(reference ** 2) / np.mean((reference - actual) ** 2)))


def test_resolved_high_notes_can_share_a_bank_frequency() -> None:
    config = AudioConfig(hybrid_residual=False)
    time = np.arange(config.sample_rate) / config.sample_rate
    source = (.3 * np.cos(2 * np.pi * 4400 * time) + .3 * np.cos(2 * np.pi * 4430 * time)).astype(np.float32)
    frames = analyse_audio(source, config, QUALITY_PROFILES["experimental"])
    preview = synthesize_preview(frames, config)
    crop = slice(4800, 43200)
    assert all(len(frame.components) == 2 for frame in frames)
    assert all(frame.components[0].frequency == frame.components[1].frequency for frame in frames)
    assert snr(source[crop], preview[crop]) > 20


@pytest.mark.parametrize("samples", [4800, 5280, 6432, 48000, 48864, 50401])
def test_tone_boundaries_do_not_fade_away(samples: int) -> None:
    config = AudioConfig(hybrid_residual=False)
    time = np.arange(samples) / config.sample_rate
    source = (.6 * np.cos(2 * np.pi * 440 * time)).astype(np.float32)
    frames = analyse_audio(source, config, QUALITY_PROFILES["normal"])
    preview = synthesize_preview(frames, config, encoded=True)
    for crop in [slice(0, 480), slice(samples - 480, samples)]:
        assert np.linalg.norm(preview[crop]) / np.linalg.norm(source[crop]) == pytest.approx(1, abs=.04)


def test_natural_fades_do_not_get_flat_boundary_envelopes() -> None:
    config = AudioConfig(hybrid_residual=False)
    time = np.arange(48000) / config.sample_rate
    source = (.6 * np.cos(2 * np.pi * 440 * time)).astype(np.float32)
    source[:2400] *= np.linspace(0, 1, 2400) ** 2
    source[-2400:] *= np.linspace(1, 0, 2400) ** 2
    frames = analyse_audio(source, config, QUALITY_PROFILES["normal"])
    preview = synthesize_preview(frames, config)
    assert preview[0] == 0
    assert all(c.envelope == "hann" for c in frames[0].components + frames[-1].components)


def test_chirp_grains_restore_vibrato_without_more_commands() -> None:
    config = AudioConfig(hybrid_residual=False)
    time = np.arange(96000) / config.sample_rate
    source = (.6 * np.cos(2 * np.pi * 440 * time - 4 * np.cos(2 * np.pi * 5 * time))).astype(np.float32)
    frames = analyse_audio(source, config, QUALITY_PROFILES["experimental"], psychoacoustic_masking=False)
    preview = synthesize_preview(frames, config, encoded=True)
    assert any(c.chirp_rate for frame in frames for c in frame.components)
    assert snr(source[9600:-9600], preview[9600:len(source) - 9600]) > 19
    assert max(len(f.components) for f in frames) <= QUALITY_PROFILES["experimental"].max_components


@pytest.mark.parametrize("correlation", [-1.0, 0.0, 0.5, 1.0])
def test_noise_preserves_stereo_coherence(correlation: float) -> None:
    config = AudioConfig()
    count = config.sample_rate * 3
    random = np.random.default_rng(71).normal(size=(count, 2))
    spectra = np.fft.rfft(random, axis=0)
    frequencies = np.fft.rfftfreq(count, 1 / config.sample_rate)
    spectra[(frequencies < 2000) | (frequencies >= 3150)] = 0
    source = np.fft.irfft(spectra, n=count, axis=0)
    source[:, 1] = correlation * source[:, 0] + np.sqrt(1 - correlation ** 2) * source[:, 1]
    source = (source * .1 / source.std()).astype(np.float32)
    quality = replace(QUALITY_PROFILES["normal"], max_components=0, max_noise_components=1, max_transient_components=0)
    frames = _analyse_channels(source, config, quality, False)
    preview = synthesize_preview(frames, config, stereo=True, encoded=True)
    assert np.corrcoef(preview[9600:-9600].T)[0, 1] == pytest.approx(correlation, abs=.16)
    assert all(len(f.residual_components) == 2 for f in frames)


@pytest.mark.parametrize("onset_ms", [211, 218, 237, 248])
def test_transients_keep_subtick_timing(onset_ms: int) -> None:
    config = AudioConfig(min_frequency=80, max_frequency=12000)
    source = np.zeros(config.sample_rate, dtype=np.float32)
    start = round(onset_ms * config.sample_rate / 1000)
    source[start:start + 64] = np.hanning(128)[:64]
    frames = analyse_audio(source, config, QUALITY_PROFILES["normal"])
    transients = [(f, c) for f in frames for c in f.residual_components if c.kind == "transient"]
    assert transients
    assert all(abs(f.index * config.hop_ms + c.delay_ms - onset_ms) <= 5 for f, c in transients)
    assert all(c.shape == "fast" for _, c in transients)


def test_transient_shapes_have_distinct_decay_and_keep_delayed_tails() -> None:
    energy_times = []
    for shape in ["fast", "medium", "slow"]:
        grain = residual_grain(48000, 4800, 7, 2000, 3150, 0, "transient", 35, shape)
        assert len(grain) == 6480
        assert not np.any(grain[:1680])
        energy = np.cumsum(grain ** 2)
        energy_times.append(np.searchsorted(energy, energy[-1] * .9))
    assert energy_times[0] < energy_times[1] < energy_times[2]


def test_new_grains_have_matching_pack_events_and_decoded_preview(tmp_path: Path) -> None:
    config = AudioConfig(min_frequency=440, max_frequency=440, phase_count=4)
    bank = tmp_path / "bank.zip"
    song = tmp_path / "song.zip"
    build_resource_pack(bank, config, DEFAULT_RESOURCE_PACK_FORMAT, grain_level=.5)
    frames = [AudioFrame(0, (Component(440, 1, .3, chirp_rate=600),), (
        ResidualComponent("transient", 3, 440, 441, 1, .1, delay_ms=35, shape="fast"),
        ResidualComponent("noise", 3, 440, 441, 2, .1, polarity=-1),
    )), AudioFrame(1, (Component(440, 2, .2, pitch=1.03, envelope="end"),))]
    build_data_pack(song, frames, "song", "wav2mc", DEFAULT_DATA_PACK_FORMAT, "modern", bank_grain_level=.5)
    with zipfile.ZipFile(bank) as archive:
        sounds = json.loads(archive.read("assets/wav2mc/sounds.json"))
    with zipfile.ZipFile(song) as archive:
        for frame in frames:
            commands = archive.read(f"data/song/function/frame/{frame.index:06d}.mcfunction").decode()
            for command in commands.splitlines():
                event = command.split("playsound ")[1].split()[0].split(":", 1)[1]
                assert event in sounds
    generated = synthesize_preview(frames, config, encoded=True, bank_grain_level=.5)
    with open_preview_bank(bank, config, "wav2mc", .5) as lookup:
        actual = synthesize_preview(frames, config, bank_lookup=lookup)
        assert np.max(np.abs(lookup("grain.f0440.p01"))) > .9
    np.testing.assert_allclose(generated, actual, atol=1e-7)
    with pytest.raises(ValueError, match="parameters"):
        with open_preview_bank(bank, replace(config, phase_count=8), "wav2mc", .5):
            pass


def test_vorbis_encoding_preserves_high_frequency_grains() -> None:
    grain = tonal_grain(48000, 4800, 19000, 0, 16)
    decoded = codec_round_trip(grain, 48000)
    assert snr(grain, decoded) > 35


def test_delayed_encoded_residual_limiter_respects_peak_ceiling() -> None:
    config = AudioConfig()
    frames = [AudioFrame(0, (), (ResidualComponent("transient", 7, 2000, 3150, 0, 2, delay_ms=45, shape="slow"),))]
    scales = _residual_frame_scales(frames, config, np.zeros(4800, dtype=np.float32), 1, 1, 1, encoded=True)
    adjusted = [replace(frames[0], residual_components=(replace(frames[0].residual_components[0], amplitude=2 * scales["transient"][0]),))]
    assert np.max(np.abs(synthesize_preview(adjusted, config, encoded=True))) <= .880001
