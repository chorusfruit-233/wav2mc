from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from wav2mc.audio import preprocess_audio
from wav2mc.config import VideoConfig
from wav2mc.utils import ensure_command
from wav2mc.video import open_video_frames


def _offset_media(tmp_path: Path, offset: float) -> Path:
    source = tmp_path / "offset.mkv"
    subprocess.run(
        [
            ensure_command("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
            "color=red:s=16x8:r=10:d=1", "-f", "lavfi", "-i",
            "sine=frequency=400:sample_rate=8000:duration=0.4",
            "-map", "0:v", "-map", "1:a", "-af", f"asetpts=PTS+({offset})/TB",
            "-c:v", "ffv1", "-c:a", "pcm_s16le", "-avoid_negative_ts", "disabled",
            "-threads", "1", str(source),
        ],
        check=True, capture_output=True,
    )
    return source


def _decode(source: Path, output: Path, **options: object) -> np.ndarray:
    preprocess_audio(
        source, output, sample_rate=8000, low_frequency=80, high_frequency=3000,
        **options,
    )
    samples, sample_rate = sf.read(output)
    assert sample_rate == 8000
    return samples


def test_video_audio_keeps_delayed_stream_as_leading_silence(tmp_path: Path) -> None:
    source = _offset_media(tmp_path, 0.3)
    samples = _decode(source, tmp_path / "synchronized.wav", preserve_timestamps=True)
    assert len(samples) / 8000 == pytest.approx(0.7, abs=0.002)
    assert np.max(np.abs(samples[:2390])) < 1e-5
    assert np.sqrt(np.mean(samples[2500:4000] ** 2)) > 0.05
    first_audible = int(np.flatnonzero(np.abs(samples) > 0.005)[0]) / 8000
    assert first_audible == pytest.approx(0.3, abs=0.002)
    with open_video_frames(source, VideoConfig(width=4, height=2, fps=10)) as video:
        assert video.duration_seconds == pytest.approx(1.0)


def test_audio_only_default_still_starts_at_first_audio_sample(tmp_path: Path) -> None:
    source = _offset_media(tmp_path, 0.3)
    default = _decode(source, tmp_path / "default.wav")
    disabled = _decode(source, tmp_path / "disabled.wav", preserve_timestamps=False)
    np.testing.assert_array_equal(default, disabled)
    assert len(default) / 8000 == pytest.approx(0.4, abs=0.002)
    assert int(np.flatnonzero(np.abs(default) > 0.005)[0]) / 8000 < 0.002


def test_negative_audio_pts_uses_same_normalized_container_origin_as_video(
    tmp_path: Path,
) -> None:
    # The container starts at -.2 because audio precedes video. FFmpeg normalizes
    # that common origin to zero for both streams, retaining the full audio and
    # moving the video's end from original PTS1.0 to normalized time1.2.
    source = _offset_media(tmp_path, -0.2)
    samples = _decode(source, tmp_path / "normalized.wav", preserve_timestamps=True)
    reference = _decode(source, tmp_path / "audio-only.wav")
    assert len(samples) / 8000 == pytest.approx(0.4, abs=0.002)
    np.testing.assert_array_equal(samples, reference)
    assert int(np.flatnonzero(np.abs(samples) > 0.005)[0]) / 8000 < 0.002
    with open_video_frames(source, VideoConfig(width=4, height=2, fps=10)) as video:
        assert video.duration_seconds == pytest.approx(1.2)
