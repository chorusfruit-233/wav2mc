from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from wav2mc.audio import load_mono, preprocess_audio
from wav2mc.cli import main
from wav2mc.utils import ensure_command


FORMATS = (
    ("mp3", "libmp3lame"),
    ("flac", "flac"),
    ("m4a", "aac"),
    ("ogg", "libvorbis"),
)


def _write_encoded_tone(
    tmp_path: Path,
    extension: str,
    encoder: str,
) -> Path:
    sample_rate = 22_050
    time = np.arange(sample_rate // 2) / sample_rate
    left = 0.5 * np.cos(2 * np.pi * 440 * time)
    right = 0.25 * np.cos(2 * np.pi * 660 * time)
    source = tmp_path / "source.wav"
    sf.write(source, np.column_stack((left, right)), sample_rate, subtype="PCM_16")

    target = tmp_path / f"input.{extension}"
    result = subprocess.run(
        [
            ensure_command("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-c:a",
            encoder,
            str(target),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"FFmpeg encoder {encoder} is unavailable")
    return target


@pytest.mark.parametrize(("extension", "encoder"), FORMATS)
def test_decodes_ffmpeg_audio_formats(
    tmp_path: Path,
    extension: str,
    encoder: str,
) -> None:
    source = _write_encoded_tone(tmp_path, extension, encoder)
    decoded = tmp_path / f"decoded_{extension}.wav"

    preprocess_audio(
        source,
        decoded,
        sample_rate=16_000,
        low_frequency=80,
        high_frequency=3000,
    )
    audio = load_mono(decoded, expected_sample_rate=16_000)

    assert audio.ndim == 1
    assert 7520 <= audio.size <= 8480
    assert float(np.max(np.abs(audio))) > 0.1


def test_convert_accepts_non_wav_input(tmp_path: Path) -> None:
    source = _write_encoded_tone(tmp_path, "m4a", "aac")
    output_dir = tmp_path / "output"

    exit_code = main(
        [
            "convert",
            str(source),
            "--name",
            "encoded_input",
            "--output-dir",
            str(output_dir),
            "--quality",
            "low",
            "--max-frequency",
            "1000",
            "--frequency-step",
            "40",
            "--phases",
            "4",
        ]
    )

    assert exit_code == 0
    assert (output_dir / "encoded_input_datapack.zip").is_file()
    assert (output_dir / "encoded_input_preview.wav").is_file()
    report_path = output_dir / "encoded_input_analysis.json"
    assert report_path.is_file()
    assert json.loads(report_path.read_text())["input_audio_stream"] == 0


def test_ffmpeg_probes_content_without_known_extension(tmp_path: Path) -> None:
    encoded = _write_encoded_tone(tmp_path, "m4a", "aac")
    source = tmp_path / "audio.unknown"
    encoded.rename(source)
    decoded = tmp_path / "decoded.wav"

    preprocess_audio(
        source,
        decoded,
        sample_rate=16_000,
        low_frequency=80,
        high_frequency=3000,
    )

    assert float(np.max(np.abs(load_mono(decoded, 16_000)))) > 0.1


def test_decode_error_includes_ffmpeg_reason(tmp_path: Path) -> None:
    source = tmp_path / "broken.mp3"
    source.write_text("not an audio file")

    with pytest.raises(
        RuntimeError,
        match="FFmpeg could not decode an audio stream",
    ):
        preprocess_audio(
            source,
            tmp_path / "decoded.wav",
            sample_rate=16_000,
            low_frequency=80,
            high_frequency=3000,
        )


def test_selects_audio_stream_from_container(tmp_path: Path) -> None:
    sample_rate = 16_000
    time = np.arange(sample_rate // 2) / sample_rate
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    sf.write(first, 0.5 * np.cos(2 * np.pi * 440 * time), sample_rate)
    sf.write(second, 0.5 * np.cos(2 * np.pi * 880 * time), sample_rate)
    container = tmp_path / "multiple_tracks.mka"
    subprocess.run(
        [
            ensure_command("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(first),
            "-i",
            str(second),
            "-map",
            "0:a:0",
            "-map",
            "1:a:0",
            "-c:a",
            "flac",
            str(container),
        ],
        check=True,
        capture_output=True,
    )
    decoded = tmp_path / "second_track.wav"

    preprocess_audio(
        container,
        decoded,
        sample_rate=sample_rate,
        low_frequency=80,
        high_frequency=2000,
        audio_stream=1,
    )
    audio = load_mono(decoded, expected_sample_rate=sample_rate)
    spectrum = np.abs(np.fft.rfft(audio))
    frequencies = np.fft.rfftfreq(audio.size, 1.0 / sample_rate)
    peak_frequency = float(frequencies[int(np.argmax(spectrum))])

    assert abs(peak_frequency - 880.0) < 5.0
