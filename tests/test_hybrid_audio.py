import json
import zipfile
from pathlib import Path

import numpy as np

from wav2mc.analysis import AudioFrame, ResidualComponent, analyse_audio
from wav2mc.bank import build_resource_pack
from wav2mc.config import (
    DEFAULT_DATA_PACK_FORMAT,
    DEFAULT_RESOURCE_PACK_FORMAT,
    AudioConfig,
    QUALITY_PROFILES,
)
from wav2mc.datapack import build_data_pack
from wav2mc.preview import synthesize_preview


def test_pure_tone_does_not_leak_into_noise_layer() -> None:
    config = AudioConfig(min_frequency=80, max_frequency=4000)
    time = np.arange(config.sample_rate) / config.sample_rate
    audio = (0.8 * np.cos(2 * np.pi * 440 * time)).astype(np.float32)

    frames = analyse_audio(audio, config, QUALITY_PROFILES["normal"])

    assert all(not frame.residual_components for frame in frames)


def test_stochastic_audio_uses_noise_residual_layer() -> None:
    config = AudioConfig(min_frequency=80, max_frequency=12000)
    rng = np.random.default_rng(19)
    audio = rng.normal(0.0, 0.2, config.sample_rate).astype(np.float32)

    frames = analyse_audio(audio, config, QUALITY_PROFILES["normal"])
    noise_components = [
        component
        for frame in frames
        for component in frame.residual_components
        if component.kind == "noise"
    ]

    assert noise_components
    assert max(
        sum(component.kind == "noise" for component in frame.residual_components)
        for frame in frames
    ) <= QUALITY_PROFILES["normal"].max_noise_components


def test_impulse_uses_short_window_transient_layer() -> None:
    config = AudioConfig(min_frequency=80, max_frequency=12000)
    audio = np.zeros(config.sample_rate, dtype=np.float32)
    start = config.sample_rate // 4
    audio[start : start + 64] = np.hanning(128)[:64]

    frames = analyse_audio(audio, config, QUALITY_PROFILES["normal"])
    transient_frames = [
        frame
        for frame in frames
        if any(
            component.kind == "transient"
            for component in frame.residual_components
        )
    ]

    assert transient_frames
    assert abs(transient_frames[0].index * config.hop_size - start) <= (
        config.hop_size // 2
    )


def test_hybrid_preview_restores_high_frequency_residual() -> None:
    config = AudioConfig(min_frequency=80, max_frequency=12000)
    time = np.arange(config.sample_rate) / config.sample_rate
    rng = np.random.default_rng(23)
    audio = (
        0.5 * np.cos(2 * np.pi * 440 * time)
        + 0.08 * rng.standard_normal(time.size)
    ).astype(np.float32)

    hybrid_frames = analyse_audio(audio, config, QUALITY_PROFILES["normal"])
    hybrid = synthesize_preview(hybrid_frames, config)[: audio.size]
    tonal_config = AudioConfig(
        min_frequency=80,
        max_frequency=12000,
        hybrid_residual=False,
    )
    tonal_frames = analyse_audio(
        audio,
        tonal_config,
        QUALITY_PROFILES["normal"],
    )
    tonal = synthesize_preview(tonal_frames, tonal_config)[: audio.size]

    frequencies = np.fft.rfftfreq(audio.size, 1.0 / config.sample_rate)
    high_band = (frequencies >= 4000) & (frequencies <= 12000)
    hybrid_energy = float(np.sum(np.abs(np.fft.rfft(hybrid))[high_band] ** 2))
    tonal_energy = float(np.sum(np.abs(np.fft.rfft(tonal))[high_band] ** 2))

    assert hybrid_energy > tonal_energy * 4.0


def test_pack_and_datapack_share_residual_event_names(tmp_path: Path) -> None:
    config = AudioConfig(
        min_frequency=400,
        max_frequency=480,
        phase_count=2,
    )
    resource_pack = tmp_path / "bank.zip"
    build_resource_pack(
        resource_pack,
        config,
        DEFAULT_RESOURCE_PACK_FORMAT,
    )
    frame = AudioFrame(
        index=0,
        components=(),
        residual_components=(
            ResidualComponent(
                kind="transient",
                band_index=3,
                low_frequency=400,
                high_frequency=481,
                variant=2,
                amplitude=0.2,
            ),
        ),
    )
    data_pack = tmp_path / "song.zip"
    build_data_pack(
        data_pack,
        [frame],
        namespace="hybrid_test",
        bank_namespace="wav2mc",
        pack_format=DEFAULT_DATA_PACK_FORMAT,
        layout="modern",
    )

    with zipfile.ZipFile(resource_pack) as archive:
        sounds = json.loads(archive.read("assets/wav2mc/sounds.json"))
    with zipfile.ZipFile(data_pack) as archive:
        command = archive.read(
            "data/hybrid_test/function/frame/000000.mcfunction"
        ).decode()

    assert "transient.b03.v02" in sounds
    assert "wav2mc:transient.b03.v02" in command
