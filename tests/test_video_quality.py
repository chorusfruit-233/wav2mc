from __future__ import annotations

import json
import subprocess
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pytest

from wav2mc import cli
from wav2mc.config import (
    DEFAULT_VIDEO_QUALITY,
    VIDEO_QUALITY_PROFILES,
    VideoConfig,
    video_config_for_quality,
    video_quality_name,
    video_renderer,
)
from wav2mc.utils import ensure_command
from wav2mc.video import VideoFrames, VideoInfo, resolve_video_config
from wav2mc.video_datapack import video_metadata


def test_video_presets_keep_screen_size_while_increasing_render_budget() -> None:
    expected = {
        "low": (64, 36, 10, 2305, 23040),
        "normal": (160, 90, 10, 14401, 144000),
        "high": (320, 180, 20, 57601, 1152000),
        "ultra": (640, 360, 20, 230401, 4608000),
    }
    assert tuple(VIDEO_QUALITY_PROFILES) == (*expected, "native")
    budgets = []
    for quality, values in expected.items():
        config = video_config_for_quality(quality)
        assert (
            config.width, config.height, config.fps,
            config.width * config.height + 1,
            config.width * config.height * config.fps,
        ) == values
        assert config.width * config.pixel_size == pytest.approx(3.2)
        assert config.height * config.pixel_size == pytest.approx(1.8)
        assert video_quality_name(config) == quality
        budgets.append(config.width * config.height * config.fps)
    assert budgets == sorted(set(budgets))
    assert DEFAULT_VIDEO_QUALITY == "normal"
    assert video_config_for_quality() == VideoConfig(160, 90, 10, 0.02)
    assert video_renderer(video_config_for_quality("low")) == "pixels"
    assert video_renderer(video_config_for_quality("ultra")) == "texture"


def test_explicit_settings_override_preset_without_recalculating_pixel_size() -> None:
    config = video_config_for_quality("high", width=24, height=16, fps=5)
    assert config == VideoConfig(24, 16, 5, 0.01)
    assert video_quality_name(config) == "custom"
    assert video_config_for_quality("ultra", pixel_size=0.3).pixel_size == 0.3
    assert video_quality_name(video_config_for_quality("ultra", pixel_size=0.3)) == "ultra"
    assert video_quality_name(video_config_for_quality("ultra", width=64, height=36, fps=10)) == "low"


@pytest.mark.parametrize("overrides", [{"width": 0}, {"fps": 12}, {"pixel_size": 0}, {"renderer": "invalid"}])
def test_video_preset_overrides_are_validated(overrides: dict) -> None:
    with pytest.raises(ValueError):
        video_config_for_quality("normal", **overrides)


def test_unknown_video_preset_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown video quality"):
        video_config_for_quality("cinema")


@pytest.mark.parametrize("quality", [*VIDEO_QUALITY_PROFILES, "custom"])
def test_metadata_reports_actual_video_quality(quality: str) -> None:
    config = VideoConfig(4, 2, 5) if quality == "custom" else video_config_for_quality(quality)
    config = resolve_video_config(config, VideoInfo(16, 9, 0.2, 0, 0, "ffv1"))
    video = VideoFrames(
        config, VideoInfo(16, 9, 0.2, 0, 0, "ffv1"),
        np.zeros((1, config.height, config.width, 3), dtype=np.uint8),
    )
    metadata = video_metadata("song", video, 121.0)
    assert metadata["quality"] == quality
    label = "自定义" if quality == "custom" else VIDEO_QUALITY_PROFILES[quality].label
    assert metadata["quality_label"] == label


@pytest.mark.parametrize("preset_first", [True, False])
def test_cli_explicit_values_override_preset_regardless_of_option_order(
    monkeypatch: pytest.MonkeyPatch, preset_first: bool,
) -> None:
    received = []
    monkeypatch.setattr(cli, "convert_audio", lambda **kwargs: received.append(kwargs) or {})
    preset = ["--video-quality", "high"]
    overrides = ["--video-width", "24", "--video-height", "16", "--video-fps", "5"]
    assert cli.main([
        "convert", "input.mp4", "--video", "--quality", "voice",
        *(preset + overrides if preset_first else overrides + preset),
    ]) == 0
    assert received[0]["video_config"] == VideoConfig(24, 16, 5, 0.01)
    assert received[0]["quality"].name == "voice"


def test_cli_quality_requires_video(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["convert", "input.wav", "--video-quality", "low"])
    assert exc.value.code == 2
    assert "--video-quality requires --video" in capsys.readouterr().err


def test_cli_video_defaults_use_aggressive_normal_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    received = []
    monkeypatch.setattr(cli, "convert_audio", lambda **kwargs: received.append(kwargs) or {})
    assert cli.main(["convert", "input.mp4", "--video"]) == 0
    assert received[0]["video_config"] == video_config_for_quality()
    assert cli.main(["convert", "input.wav"]) == 0
    assert received[1]["video_config"] is None


def test_cli_native_preserves_deferred_dimensions_and_texture_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    received = []
    monkeypatch.setattr(cli, "convert_audio", lambda **kwargs: received.append(kwargs) or {})
    assert cli.main([
        "convert", "input.mp4", "--video", "--video-quality", "native",
        "--video-renderer", "texture", "--video-resource-pack-format", "13",
    ]) == 0
    requested = received[0]["video_config"]
    assert requested == video_config_for_quality("native", renderer="texture")
    assert requested.width is None and requested.height is None
    assert received[0]["video_resource_pack_format"] == 13


def test_cli_rejects_ambiguous_single_native_dimension(capsys: pytest.CaptureFixture) -> None:
    assert cli.main([
        "convert", "input.mp4", "--video", "--video-quality", "native", "--video-width", "1280",
    ]) == 2
    assert "both video width and height" in capsys.readouterr().err


def test_cli_low_quality_converts_and_matches_data_pack_metadata(tmp_path: Path) -> None:
    source = tmp_path / "clip.mkv"
    subprocess.run([
        ensure_command("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
        "color=c=blue:s=16x8:r=10:d=0.2", "-c:v", "ffv1", str(source),
    ], check=True, capture_output=True)
    output_dir = tmp_path / "output"
    assert cli.main([
        "convert", str(source), "--video", "--video-quality", "low", "--no-audio",
        "--name", "graded", "--output-dir", str(output_dir),
    ]) == 0
    report = json.loads((output_dir / "graded_analysis.json").read_text())
    assert report["video"]["quality"] == "low"
    assert report["video"]["quality_label"] == "低"
    assert report["video"]["width"] == 64
    assert report["video"]["height"] == 36
    assert report["video"]["fps"] == 10
    assert report["video"]["pixel_entities"] == 2304
    assert report["video"]["pixel_updates_per_second"] == 23040
    with ZipFile(output_dir / "graded_datapack.zip") as archive:
        assert archive.testzip() is None
        assert json.loads(archive.read("wav2mc-song.json"))["video"] == report["video"]
