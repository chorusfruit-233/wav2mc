from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from wav2mc.config import (
    VideoConfig, video_config_for_quality, video_quality_name, video_renderer,
)
from wav2mc.utils import ensure_command
from wav2mc.video import VideoInfo, decode_video, probe_video, resolve_video_config


def test_native_config_resolves_source_dimensions_without_old_pixel_limit() -> None:
    requested = video_config_for_quality("native")
    assert requested.width is None and requested.height is None
    assert requested.pixel_size is None
    assert video_renderer(requested) == "texture"
    resolved = resolve_video_config(requested, VideoInfo(1920, 1080, 1, 0, 0, "h264"))
    assert (resolved.width, resolved.height, resolved.fps) == (1920, 1080, 20)
    assert resolved.width * resolved.pixel_size == pytest.approx(3.2)
    assert video_quality_name(resolved) == "native"
    resolved.validate()


def test_native_resolution_override_is_explicit_and_validated() -> None:
    custom = video_config_for_quality("native", width=1280, height=720, fps=10)
    assert not custom.native_resolution
    assert custom.pixel_size == pytest.approx(3.2 / 1280)
    assert video_quality_name(custom) == "custom"
    with pytest.raises(ValueError, match="both video width and height"):
        video_config_for_quality("native", width=1280)
    with pytest.raises(ValueError, match="Video fps"):
        video_config_for_quality("native", fps=60)
    with pytest.raises(ValueError, match="4096"):
        resolve_video_config(
            video_config_for_quality("native", renderer="pixels"),
            VideoInfo(1920, 1080, 1, 0, 0, "h264"),
        )
    with pytest.raises(ValueError, match="positive integers"):
        VideoConfig(width=None).validate()


def test_native_decode_preserves_every_source_pixel_without_spatial_resampling(tmp_path: Path) -> None:
    source = tmp_path / "lossless-rgb.mkv"
    subprocess.run([
        ensure_command("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "testsrc=s=130x70:r=4:d=0.5", "-c:v", "ffv1", "-pix_fmt", "bgr0", str(source),
    ], check=True, capture_output=True)
    reference = subprocess.run([
        ensure_command("ffmpeg"), "-v", "error", "-i", str(source), "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ], check=True, capture_output=True).stdout
    decoded = decode_video(source, video_config_for_quality("native"))
    assert decoded.frames.shape == (10, 70, 130, 3)
    np.testing.assert_array_equal(decoded.frames[0], np.frombuffer(reference, dtype=np.uint8).reshape(70, 130, 3))
    assert (decoded.config.width, decoded.config.height) == (130, 70)
    assert video_quality_name(decoded.config) == "native"
    assert video_renderer(decoded.config) == "texture"


def test_native_autorotation_resolves_the_decoded_pixel_raster(tmp_path: Path) -> None:
    original = tmp_path / "original.mp4"
    rotated = tmp_path / "rotated.mp4"
    subprocess.run([
        ensure_command("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "color=red:s=16x8:r=4:d=0.5", "-c:v", "libx264", str(original),
    ], check=True, capture_output=True)
    subprocess.run([
        ensure_command("ffmpeg"), "-v", "error", "-display_rotation", "90", "-i", str(original),
        "-c", "copy", str(rotated),
    ], check=True, capture_output=True)
    info = probe_video(rotated)
    assert (info.width, info.height) == (16, 8)
    assert (info.display_width, info.display_height) == (8, 16)
    decoded = decode_video(rotated, video_config_for_quality("native", fps=4))
    assert decoded.frames.shape == (2, 16, 8, 3)
    assert (decoded.config.width, decoded.config.height) == (8, 16)
    assert np.min(decoded.frames[..., 0]) > 240


def test_native_unsupported_rotation_is_rejected_before_decode() -> None:
    with pytest.raises(ValueError, match="multiples of 90"):
        resolve_video_config(video_config_for_quality("native"), VideoInfo(100, 50, 1, 0, 0, "h264", 45))
    assert resolve_video_config(VideoConfig(), VideoInfo(100, 50, 1, 0, 0, "h264", 45)) == VideoConfig()


@pytest.mark.parametrize("renderer", ["auto", "texture"])
def test_full_hd_custom_dimensions_are_accepted(renderer: str) -> None:
    config = replace(VideoConfig(), width=1920, height=1080, renderer=renderer)
    config.validate()
    assert video_renderer(config) == "texture"


def test_native_anamorphic_pixels_keep_raster_and_display_aspect(tmp_path: Path) -> None:
    source = tmp_path / "anamorphic.mkv"
    subprocess.run([
        ensure_command("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "testsrc=s=130x70:r=4:d=0.5", "-vf", "setsar=2/1",
        "-c:v", "ffv1", "-pix_fmt", "bgr0", str(source),
    ], check=True, capture_output=True)
    info = probe_video(source)
    assert info.sample_aspect_ratio == 2
    decoded = decode_video(source, video_config_for_quality("native", fps=4))
    assert decoded.frames.shape == (2, 70, 130, 3)
    assert decoded.config.pixel_aspect_ratio == 2
    assert decoded.config.pixel_height == pytest.approx(decoded.config.pixel_size / 2)
    rotated = resolve_video_config(video_config_for_quality("native"), replace(info, rotation=90))
    assert (rotated.width, rotated.height) == (70, 130)
    assert rotated.pixel_aspect_ratio == 0.5


@pytest.mark.parametrize("ratio", [0, -1, float("inf"), float("nan"), True, "2"])
def test_invalid_pixel_aspect_ratio_is_rejected(ratio: object) -> None:
    with pytest.raises(ValueError, match="aspect ratio"):
        VideoConfig(pixel_aspect_ratio=ratio).validate()
