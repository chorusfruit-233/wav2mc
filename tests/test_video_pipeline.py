from __future__ import annotations

import json
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest

from wav2mc.cli import main
from wav2mc.config import AudioConfig, QUALITY_PROFILES, VideoConfig
from wav2mc.pipeline import convert_audio
from wav2mc.utils import TaskCancelled, conversion_output_paths, ensure_command


def make_clip(path: Path, audio: bool) -> Path:
    command = [
        ensure_command("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
        "color=c=red:s=16x8:r=10:d=0.6",
    ]
    if audio:
        command.extend([
            "-f", "lavfi", "-i", "sine=frequency=400:sample_rate=8000:duration=0.3",
            "-c:a", "pcm_s16le",
        ])
    command.extend(["-c:v", "ffv1", str(path)])
    subprocess.run(command, check=True, capture_output=True)
    return path


def conversion_options(source: Path, output_dir: Path) -> dict:
    return dict(
        source=source, output_dir=output_dir, song_name="视频测试",
        config=AudioConfig(
            sample_rate=8000, min_frequency=100, max_frequency=500,
            frequency_step=100, phase_count=2, adaptive_frequency_grid=False,
            hybrid_residual=False,
        ),
        quality=QUALITY_PROFILES["low"], data_pack_format=121.0,
        layout="modern", bank_namespace="wav2mc", category="record",
        requested_gain=1.0, bank_grain_level=1.0,
        video_config=VideoConfig(width=4, height=2, fps=10),
    )


@pytest.mark.parametrize(("has_audio", "keep_audio"), [(True, True), (True, False), (False, True)])
def test_video_conversion_syncs_timeline_and_publishes_matching_metadata(
    tmp_path: Path, has_audio: bool, keep_audio: bool,
) -> None:
    source = make_clip(tmp_path / "input.mkv", has_audio)
    updates = []
    options = conversion_options(source, tmp_path / "output")
    outputs = convert_audio(**options, include_audio=keep_audio, progress_callback=updates.append)
    report = json.loads(outputs["report"].read_text())
    assert report["audio_enabled"] == (has_audio and keep_audio)
    assert bool(report["required_resource_pack"]) == (has_audio and keep_audio)
    assert report["video"]["frame_count"] == 6
    assert report["video"]["pixel_entities"] == 8
    assert report["frame_count"] == report["playback"]["duration_ticks"] == 12
    assert report["playback"]["duration_seconds"] == 0.6
    fractions = [update.fraction for update in updates]
    assert fractions == sorted(fractions)
    assert fractions[-1] == 1.0
    assert "video_decode" in {update.stage for update in updates}
    with ZipFile(outputs["data_pack"]) as archive:
        assert archive.testzip() is None
        song = json.loads(archive.read("wav2mc-song.json"))
        assert report["video"] == song["video"]
        assert report["playback"] == song["playback"]
        for command in report["playback"]["commands"].values():
            function = command.removeprefix("/function ").replace(":", "/function/", 1)
            assert f"data/{function}.mcfunction" in archive.namelist()
        namespace = report["song_namespace"]
        final_audio_frame = archive.read(f"data/{namespace}/function/frame/000011.mcfunction").decode()
        assert "playsound" not in final_audio_frame
    assert outputs == conversion_output_paths(tmp_path / "output", "视频测试")
    assert not list((tmp_path / "output").glob(".wav2mc-*"))


def test_video_cancellation_preserves_all_previous_outputs(tmp_path: Path) -> None:
    source = make_clip(tmp_path / "input.mkv", False)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    existing = conversion_output_paths(output_dir, "视频测试")
    for path in existing.values():
        path.write_bytes(b"previous output")
    cancelled = False

    def progress(update: object) -> None:
        nonlocal cancelled
        if getattr(update, "stage", None) == "datapack":
            cancelled = True

    with pytest.raises(TaskCancelled):
        convert_audio(
            **conversion_options(source, output_dir),
            progress_callback=progress, cancel_check=lambda: cancelled,
        )
    assert all(path.read_bytes() == b"previous output" for path in existing.values())
    assert sorted(output_dir.iterdir()) == sorted(existing.values())


def test_cli_converts_silent_video_with_screen_controls(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    source = make_clip(tmp_path / "silent.mkv", False)
    output_dir = tmp_path / "output"
    assert main([
        "convert", str(source), "--video", "--no-audio", "--name", "clip",
        "--output-dir", str(output_dir), "--video-width", "4", "--video-height", "2",
        "--video-fps", "5", "--video-pixel-size", "0.2",
    ]) == 0
    report = json.loads((output_dir / "clip_analysis.json").read_text())
    assert report["video"]["fps"] == 5
    assert report["video"]["pixel_size_blocks"] == 0.2
    assert report["required_resource_pack"] is None
    assert "Place screen: /function clip:video/screen_create" in capsys.readouterr().out


def test_audio_cannot_be_disabled_without_video(tmp_path: Path) -> None:
    source = make_clip(tmp_path / "input.mkv", True)
    options = conversion_options(source, tmp_path / "output")
    options["video_config"] = None
    with pytest.raises(ValueError, match="requires video"):
        convert_audio(**options, include_audio=False)
    assert not (tmp_path / "output").exists()


def test_conversion_preserves_delayed_audio_on_video_timeline(tmp_path: Path) -> None:
    source = tmp_path / "delayed.mkv"
    subprocess.run([
        ensure_command("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
        "color=c=red:s=16x8:r=10:d=1", "-itsoffset", "0.3", "-f", "lavfi", "-i",
        "sine=frequency=400:sample_rate=8000:duration=0.4",
        "-c:v", "ffv1", "-c:a", "pcm_s16le", str(source),
    ], check=True, capture_output=True)
    outputs = convert_audio(**conversion_options(source, tmp_path / "output"))
    report = json.loads(outputs["report"].read_text())
    assert report["input_duration_seconds"] == pytest.approx(0.7, abs=0.01)
    assert report["playback"]["duration_seconds"] == 1.0
    with ZipFile(outputs["data_pack"]) as archive:
        first_sound_frame = min(
            int(Path(name).stem)
            for name in archive.namelist()
            if "/frame/" in name and "/video/" not in name
            and "playsound" in archive.read(name).decode()
        )
    assert 4 <= first_sound_frame <= 6
