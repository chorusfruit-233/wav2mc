from __future__ import annotations

import json
import struct
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest

from wav2mc.cli import main
from wav2mc.config import AudioConfig, QUALITY_PROFILES, video_config_for_quality
from wav2mc.pipeline import convert_audio
from wav2mc.utils import TaskCancelled, conversion_output_paths, ensure_command


def make_clip(path: Path, size: str = "320x180") -> Path:
    subprocess.run([
        ensure_command("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        f"testsrc2=size={size}:rate=20:duration=0.1", "-c:v", "ffv1", str(path),
    ], check=True, capture_output=True)
    return path


def options(source: Path, output: Path) -> dict:
    return dict(
        source=source, output_dir=output, song_name="native",
        config=AudioConfig(), quality=QUALITY_PROFILES["low"],
        data_pack_format=121.0, layout="modern", bank_namespace="wav2mc",
        category="record", requested_gain=1.0, bank_grain_level=1.0,
        video_config=video_config_for_quality("native"), include_audio=False,
    )


def test_cli_full_hd_native_produces_matching_data_and_texture_packs(tmp_path: Path) -> None:
    source = make_clip(tmp_path / "fullhd.mkv", "1920x1080")
    output = tmp_path / "output"
    assert main([
        "convert", str(source), "--video", "--video-quality", "native",
        "--no-audio", "--name", "native", "--output-dir", str(output),
    ]) == 0
    paths = conversion_output_paths(output, "native", include_video_resource_pack=True)
    assert all(path.is_file() for path in paths.values())
    report = json.loads(paths["report"].read_text())
    video = report["video"]
    assert (video["width"], video["height"], video["quality"]) == (1920, 1080, "native")
    assert video["native_resolution"] is True
    assert video["requires_resource_pack"] is True
    assert video["display_entities"] == 40
    assert video["entity_count"] == 41
    assert video["display_updates_per_second"] == 800
    assert video["frame_count"] == 2
    assert report["required_resource_pack"] is None
    assert report["outputs"] == {key: path.name for key, path in paths.items()}
    with ZipFile(paths["data_pack"]) as data, ZipFile(paths["video_resource_pack"]) as assets:
        assert data.testzip() is None and assets.testzip() is None
        assert json.loads(data.read("wav2mc-song.json"))["video"] == video
        assert json.loads(assets.read("wav2mc-video.json"))["video"] == video
        assert "*_video_resourcepack.zip" in data.read("README.txt").decode()
        spawn = data.read("data/native/function/video/internal/spawn_pixels.mcfunction").decode()
        assert spawn.count("summon minecraft:text_display") == 40
        font = json.loads(assets.read("assets/native/font/video/frame_000000.json"))["providers"][0]
        assert (font["height"], font["ascent"]) == (8, 7)
        assert [len(row) for row in font["chars"]] == [8] * 5
        png = assets.read("assets/native/textures/video/frame_000000.png")
        assert struct.unpack(">II", png[16:24]) == (2048, 1280)


def test_texture_progress_is_monotonic_and_resource_format_is_selected(tmp_path: Path) -> None:
    source = make_clip(tmp_path / "clip.mkv")
    updates = []
    paths = convert_audio(
        **options(source, tmp_path / "output"), video_resource_pack_format=88.0,
        progress_callback=updates.append,
    )
    fractions = [update.fraction for update in updates]
    assert fractions == sorted(fractions)
    assert fractions[-1] == 1
    assert "video_resource_pack" in {update.stage for update in updates}
    with ZipFile(paths["video_resource_pack"]) as archive:
        assert json.loads(archive.read("pack.mcmeta"))["pack"]["pack_format"] == 88.0


def test_resource_generation_cancellation_preserves_all_four_outputs(tmp_path: Path) -> None:
    source = make_clip(tmp_path / "clip.mkv")
    output = tmp_path / "output"
    output.mkdir()
    paths = conversion_output_paths(output, "native", include_video_resource_pack=True)
    for path in paths.values():
        path.write_bytes(b"previous result")
    cancelled = False

    def progress(update: object) -> None:
        nonlocal cancelled
        if getattr(update, "stage", None) == "video_resource_pack":
            cancelled = True

    with pytest.raises(TaskCancelled):
        convert_audio(
            **options(source, output), progress_callback=progress,
            cancel_check=lambda: cancelled,
        )
    assert all(path.read_bytes() == b"previous result" for path in paths.values())
    assert sorted(output.iterdir()) == sorted(paths.values())


def test_commit_failure_restores_all_outputs_including_video_resource_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_clip(tmp_path / "clip.mkv")
    output = tmp_path / "output"
    output.mkdir()
    paths = conversion_output_paths(output, "native", include_video_resource_pack=True)
    for path in paths.values():
        path.write_bytes(b"previous result")
    original_replace = Path.replace

    def fail_resource_commit(path: Path, target: Path):
        if target == paths["video_resource_pack"] and path.parent.name.startswith(".wav2mc-convert-"):
            raise OSError("simulated resource-pack commit failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_resource_commit)
    with pytest.raises(OSError, match="commit failure"):
        convert_audio(**options(source, output))
    assert all(path.read_bytes() == b"previous result" for path in paths.values())
    assert sorted(output.iterdir()) == sorted(paths.values())
