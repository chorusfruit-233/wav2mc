from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from wav2mc.config import VideoConfig
from wav2mc.utils import TaskCancelled, ensure_command
from wav2mc.video import decode_video, open_video_frames, probe_video


def _video(tmp_path: Path, duration: float = 0.5) -> Path:
    source = tmp_path / "tiny red video.mkv"
    subprocess.run(
        [
            ensure_command("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
            "-y", "-f", "lavfi", "-i", f"color=c=red:s=16x8:r=4:d={duration}",
            "-an", "-c:v", "ffv1", "-threads", "1", str(source),
        ],
        check=True, capture_output=True,
    )
    return source


def test_probe_video_reports_selected_stream_and_duration(tmp_path: Path) -> None:
    info = probe_video(_video(tmp_path))
    assert (info.width, info.height, info.video_stream, info.stream_index) == (16, 8, 0, 0)
    assert info.duration == pytest.approx(0.5)
    assert info.codec == "ffv1"


def test_decode_rgb_preserves_aspect_with_black_letterbox_and_tick_rate(tmp_path: Path) -> None:
    source = _video(tmp_path)
    config = VideoConfig(width=8, height=8, fps=4)
    updates = []
    with open_video_frames(source, config, progress_callback=updates.append) as video:
        assert isinstance(video.frames, np.memmap)
        assert video.frames.shape == (2, 8, 8, 3)
        assert video.frames.dtype == np.uint8
        assert video.frame_count == 2
        assert video.tick_count == 10
        assert video.duration_seconds == pytest.approx(0.5)
        assert np.max(video.frames[:, :2]) == 0
        assert np.max(video.frames[:, -2:]) == 0
        assert np.min(video.frames[:, 2:6, :, 0]) >= 240
        assert np.max(video.frames[:, 2:6, :, 1:]) <= 5
        mapping = video.frames._mmap
        raw_path = Path(video.frames.filename)
        assert raw_path.is_file()
    assert mapping.closed
    assert not raw_path.exists()
    assert [update.fraction for update in updates] == sorted(
        update.fraction for update in updates
    )
    assert updates[0].fraction == 0 and updates[-1].fraction == 1
    assert {update.stage for update in updates} == {"video_decode"}


def test_decode_video_copy_remains_usable_after_temporary_cleanup(tmp_path: Path) -> None:
    decoded = decode_video(_video(tmp_path), VideoConfig(width=4, height=2, fps=20))
    assert not isinstance(decoded.frames, np.memmap)
    assert decoded.frames.shape == (10, 2, 4, 3)
    assert decoded.tick_count == 10
    assert decoded.frames[0, 0, 0, 0] > 240


def test_very_short_clip_keeps_its_first_frame(tmp_path: Path) -> None:
    source = tmp_path / "one-frame.mkv"
    subprocess.run(
        [ensure_command("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
         "color=red:s=16x8:r=100:d=0.01", "-c:v", "ffv1", str(source)],
        check=True, capture_output=True,
    )
    decoded = decode_video(source, VideoConfig())
    assert decoded.frame_count == 1
    assert decoded.tick_count == 2


def test_selected_video_stream_is_decoded_instead_of_first(tmp_path: Path) -> None:
    source = tmp_path / "two-streams.mkv"
    subprocess.run(
        [ensure_command("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
         "color=red:s=16x8:r=4:d=0.5", "-f", "lavfi", "-i",
         "color=blue:s=16x8:r=4:d=0.5", "-map", "0:v", "-map", "1:v",
         "-c:v", "ffv1", "-threads", "1", str(source)],
        check=True, capture_output=True,
    )
    decoded = decode_video(source, VideoConfig(width=4, height=2, fps=4), video_stream=1)
    assert decoded.info.video_stream == 1 and decoded.info.stream_index == 1
    assert decoded.frames[0, 0, 0, 2] > 240
    assert decoded.frames[0, 0, 0, 0] < 5


def test_autorotation_is_applied_before_letterboxing(tmp_path: Path) -> None:
    original = tmp_path / "original.mp4"
    rotated = tmp_path / "rotated.mp4"
    subprocess.run(
        [ensure_command("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
         "color=red:s=16x8:r=4:d=0.5", "-c:v", "libx264", "-threads", "1", str(original)],
        check=True, capture_output=True,
    )
    subprocess.run(
        [ensure_command("ffmpeg"), "-v", "error", "-display_rotation", "90", "-i", str(original),
         "-c", "copy", str(rotated)],
        check=True, capture_output=True,
    )
    decoded = decode_video(rotated, VideoConfig(width=8, height=8, fps=4))
    assert np.max(decoded.frames[:, :, :2]) == 0
    assert np.max(decoded.frames[:, :, -2:]) == 0
    assert np.min(decoded.frames[:, :, 2:6, 0]) > 240


def test_single_pixel_output_does_not_round_scale_dimension_to_zero(tmp_path: Path) -> None:
    decoded = decode_video(_video(tmp_path), VideoConfig(width=1, height=1, fps=4))
    assert decoded.frames.shape == (2, 1, 1, 3)
    assert decoded.frames[0, 0, 0, 0] > 240


def test_video_stream_start_offset_is_filled_to_match_media_timeline(tmp_path: Path) -> None:
    source = tmp_path / "delayed video.mkv"
    subprocess.run(
        [ensure_command("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
         "color=red:s=16x8:r=4:d=0.5", "-f", "lavfi", "-i",
         "anullsrc=r=8000:cl=mono", "-map", "0:v", "-map", "1:a",
         "-vf", "setpts=PTS+0.5/TB", "-c:v", "ffv1", "-c:a", "pcm_s16le",
         "-t", "1", "-threads", "1", str(source)],
        check=True, capture_output=True,
    )
    decoded = decode_video(source, VideoConfig(width=4, height=2, fps=4))
    assert decoded.frame_count == 4
    assert decoded.duration_seconds == 1.0
    assert np.min(decoded.frames[:, :, :, 0]) > 240


@pytest.mark.parametrize("changes", [
    {"width": 0}, {"height": -1}, {"width": None}, {"width": True},
    {"width": 64, "height": 65, "renderer": "pixels"}, {"fps": 3}, {"fps": 0}, {"fps": True},
    {"fps": 10.0}, {"pixel_size": 0}, {"pixel_size": 2.1},
    {"pixel_size": float("nan")}, {"pixel_size": float("inf")},
])
def test_video_config_rejects_invalid_or_excessive_settings(changes: dict) -> None:
    with pytest.raises(ValueError):
        replace(VideoConfig(), **changes).validate()


def test_video_config_defaults_and_maximum_pixel_budget() -> None:
    config = VideoConfig()
    config.validate()
    assert (config.width, config.height, config.fps, config.pixel_size) == (32, 18, 10, 0.1)
    VideoConfig(width=64, height=64, fps=20).validate()
    VideoConfig(width=1920, height=1080, renderer="texture").validate()
    VideoConfig(width=1920, height=1080).validate()
    assert config.ticks_per_frame == 2


def test_probe_rejects_audio_only_missing_stream_and_missing_file(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    subprocess.run(
        [ensure_command("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=0.1", str(audio)],
        check=True, capture_output=True,
    )
    with pytest.raises(RuntimeError, match="No video stream"):
        probe_video(audio)
    with pytest.raises(RuntimeError, match="No video stream"):
        probe_video(_video(tmp_path), 1)
    with pytest.raises(FileNotFoundError):
        probe_video(tmp_path / "missing.mp4")
    with pytest.raises(ValueError, match="nonnegative integer"):
        probe_video(audio, -1)


def test_probe_error_includes_real_ffmpeg_reason(tmp_path: Path) -> None:
    source = tmp_path / "bad.mp4"
    source.write_bytes(b"not a video")
    with pytest.raises(RuntimeError, match="FFprobe could not read video.*") as failure:
        probe_video(source)
    assert "Invalid data" in str(failure.value) or "moov atom" in str(failure.value)


def test_cancellation_during_decode_terminates_child_and_removes_raw_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _video(tmp_path)
    real_popen = subprocess.Popen
    processes = []
    stopped = False

    def spawn(*args: object, **kwargs: object) -> subprocess.Popen:
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    def update(_progress: object) -> None:
        nonlocal stopped
        stopped = True

    monkeypatch.setattr("wav2mc.video.subprocess.Popen", spawn)
    with pytest.raises(TaskCancelled):
        with open_video_frames(
            source, VideoConfig(), progress_callback=update, cancel_check=lambda: stopped,
        ):
            pytest.fail("Cancelled decoder must not yield")
    # ffprobe uses subprocess.run and can also pass through the patched Popen.
    assert processes
    assert all(process.poll() is not None for process in processes)
    raw_outputs = [Path(process.args[-1]) for process in processes if "-f" in process.args]
    assert raw_outputs and all(not path.exists() for path in raw_outputs)


@pytest.mark.parametrize("payload", [[], {"streams": "invalid"}, {
    "streams": [{"codec_type": "video", "width": 0, "height": 1, "index": 0}],
}])
def test_probe_rejects_invalid_descriptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: object,
) -> None:
    source = tmp_path / "dummy"
    source.touch()
    monkeypatch.setattr("wav2mc.video.subprocess.run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps(payload), stderr="",
    ))
    with pytest.raises(RuntimeError, match="invalid video description"):
        probe_video(source)


def test_probe_rejects_attached_cover_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "covered audio.m4a"
    source.touch()
    monkeypatch.setattr("wav2mc.video.subprocess.run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps({"streams": [{
            "codec_type": "video", "width": 16, "height": 16, "index": 1,
            "disposition": {"attached_pic": 1},
        }]}), stderr="",
    ))
    with pytest.raises(RuntimeError, match="attached cover"):
        probe_video(source)


@pytest.mark.parametrize(("raw", "returncode", "error", "expected"), [
    (b"", 0, b"", "no video frames"),
    (b"1", 0, b"", "incomplete RGB"),
    (b"", 2, b"broken decoder", "broken decoder"),
])
def test_failed_or_partial_decode_is_rejected_and_temporary_data_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    raw: bytes, returncode: int, error: bytes, expected: str,
) -> None:
    source = _video(tmp_path)
    info = probe_video(source)
    monkeypatch.setattr("wav2mc.video.probe_video", lambda *_args: info)
    raw_paths = []

    def spawn(command: list[str], **kwargs: object) -> SimpleNamespace:
        raw_path = Path(command[-1])
        raw_paths.append(raw_path)
        raw_path.write_bytes(raw)
        kwargs["stderr"].write(error)
        return SimpleNamespace(returncode=returncode, poll=lambda: returncode, wait=lambda: returncode)

    monkeypatch.setattr("wav2mc.video.subprocess.Popen", spawn)
    with pytest.raises(RuntimeError, match=expected):
        with open_video_frames(source, VideoConfig(width=2, height=2)):
            pytest.fail("Invalid decoded data must not yield")
    assert raw_paths and not raw_paths[0].exists()
