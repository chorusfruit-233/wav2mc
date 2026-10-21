from __future__ import annotations

import json
import re
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pytest

from wav2mc.analysis import AudioFrame
from wav2mc.config import VideoConfig
from wav2mc.datapack import build_data_pack
from wav2mc.utils import TaskCancelled
from wav2mc.video import VideoFrames, VideoInfo
from wav2mc.video_datapack import VIDEO_DIMENSIONS, video_metadata


def make_video(frame_count: int = 3, width: int = 2, height: int = 2) -> VideoFrames:
    frames = np.arange(frame_count * height * width * 3, dtype=np.uint8).reshape(
        frame_count, height, width, 3,
    )
    frames[0, 0, 0] = (255, 0, 0)
    return VideoFrames(
        VideoConfig(width, height, 10, 0.25),
        VideoInfo(width, height, frame_count / 10, 0, 0, "rawvideo"), frames,
    )


def make_pack(
    tmp_path: Path, video: VideoFrames, namespace: str = "video_song",
    layout: str = "modern", pack_format: float = 121.0,
) -> tuple[dict[str, object], dict[str, list[str]]]:
    target = tmp_path / f"{namespace}.zip"
    build_data_pack(
        target, [AudioFrame(index, ()) for index in range(video.tick_count + 3)],
        namespace, "wav2mc", pack_format, layout, video=video,
    )
    with ZipFile(target) as archive:
        metadata = json.loads(archive.read("wav2mc-song.json"))
        functions = {}
        for path in archive.namelist():
            match = re.fullmatch(r"data/[^/]+/functions?/(.+)\.mcfunction", path)
            if match:
                functions[match[1]] = archive.read(path).decode().splitlines()
    return metadata, functions


class RenderTimeline:
    """Interpret the generated render/dispatch subset to check real frame choices."""

    def __init__(self, metadata: dict[str, object], functions: dict[str, list[str]]) -> None:
        video = metadata["video"]
        self.namespace = metadata["namespace"]
        self.functions = functions
        self.scores = {
            "#frame": 0, "#vinterval": 20 // video["fps"],
            "#vwanted": 0, "#vshown": -1,
        }
        self.colors: dict[int, int] = {}
        self.dimension = "minecraft:overworld"
        self.drawn = []

    @staticmethod
    def matches(value: int, match: str) -> bool:
        if ".." not in match:
            return value == int(match)
        lower, upper = match.split("..")
        return (not lower or value >= int(lower)) and (not upper or value <= int(upper))

    def run(self, name: str) -> None:
        if name.startswith("video/frame/"):
            self.drawn.append(int(name.rsplit("/", 1)[1]))
        for line in self.functions[name]:
            if line.startswith("function "):
                callee = line.partition(":")[2]
                if callee != "video/clean":
                    self.run(callee)
            elif line.startswith("scoreboard players operation"):
                _, _, _, target, _objective, operator, source, _source_objective = line.split()
                if operator == "=":
                    self.scores[target] = self.scores[source]
                elif operator == "/=":
                    self.scores[target] //= self.scores[source]
                else:
                    raise AssertionError(line)
            elif line.startswith("execute if score") or line.startswith("execute unless score"):
                condition, command = line.split(" run ", 1)
                tokens = condition.split()
                if tokens[5] == "matches":
                    matched = self.matches(self.scores[tokens[3]], tokens[6])
                else:
                    assert tokens[5] == "="
                    matched = self.scores[tokens[3]] == self.scores[tokens[6]]
                if matched != (tokens[1] == "unless"):
                    if command.startswith("scoreboard players set"):
                        fields = command.split()
                        self.scores[fields[3]] = int(fields[5])
                    elif command.startswith("function "):
                        self.run(command.partition(":")[2])
                    else:
                        raise AssertionError(line)
            elif line.startswith("execute in "):
                dimension = line.split()[2]
                if dimension == self.dimension:
                    self.run(line.split(" run function ")[1].partition(":")[2])
            elif line.startswith("data merge entity"):
                match = re.search(r"_p(\d+),limit=1\] \{background:(-?\d+)\}", line)
                assert match is not None
                self.colors[int(match[1])] = int(match[2])
            else:
                raise AssertionError(line)

    def render(self, tick: int) -> list[int]:
        self.scores["#frame"] = tick
        self.run("video/render")
        return [self.colors[index] & 0xFFFFFF for index in sorted(self.colors)]


@pytest.mark.parametrize("layout", ["modern", "legacy"])
def test_generated_controls_and_metadata_are_complete(tmp_path: Path, layout: str) -> None:
    video = make_video()
    metadata, functions = make_pack(tmp_path, video, layout=layout)
    screen = metadata["video"]
    assert screen == video_metadata("video_song", video, 121.0)
    assert screen["entity_count"] == 5
    assert screen["screen_width_blocks"] == screen["screen_height_blocks"] == 0.5
    assert screen["requires_resource_pack"] is False
    assert screen["supported_dimensions"] == list(VIDEO_DIMENSIONS)
    assert metadata["playback"]["commands"]["screen_create"] == screen["commands"]["screen_create"]
    for command in metadata["playback"]["commands"].values():
        assert command.partition(":")[2] in functions


def test_full_rgb_rendering_and_seek_dispatch_match_video_samples(tmp_path: Path) -> None:
    video = make_video(frame_count=35)
    metadata, functions = make_pack(tmp_path, video)
    timeline = RenderTimeline(metadata, functions)
    for tick, expected_index in [(0, 0), (1, 0), (2, 1), (34, 17), (69, 34), (1000, 34), (-1, 0)]:
        rgb = video.frames[expected_index].reshape(-1, 3).astype(np.uint32)
        expected = (rgb[:, 0] << 16) | (rgb[:, 1] << 8) | rgb[:, 2]
        assert timeline.render(tick) == expected.tolist()
        assert timeline.drawn[-1] == expected_index
    assert -65536 in [int(value) for value in re.findall(r"background:(-?\d+)", "\n".join(functions["video/frame/000000"]))]


def test_video_updates_only_on_sample_ticks_and_controls_refresh_seek(tmp_path: Path) -> None:
    video = make_video()
    _metadata, functions = make_pack(tmp_path, video)
    for tick in range(video.tick_count + 3):
        rendered = "function video_song:video/render" in functions[f"frame/{tick:06d}"]
        assert rendered == (tick < video.tick_count and tick % 2 == 0)
    assert functions["internal/seek"][-1] == "function video_song:video/render"
    assert functions["internal/resume"][-1] == "function video_song:video/render"
    assert "function video_song:video/render" not in functions["internal/pause"]
    assert "function video_song:video/screen_remove" not in functions["finish"]
    assert functions["stop"][-1] == "function video_song:video/screen_remove"
    assert functions["load"][-1] == "function video_song:video/screen_remove"


def test_paused_unloaded_seek_refreshes_when_screen_returns_without_advancing_video(
    tmp_path: Path,
) -> None:
    video = make_video()
    metadata, functions = make_pack(tmp_path, video)
    timeline = RenderTimeline(metadata, functions)
    timeline.render(0)
    assert timeline.scores["#vshown"] == 0
    timeline.dimension = None
    timeline.render(4)
    assert timeline.scores["#vwanted"] == 2
    assert timeline.scores["#vshown"] == 0
    timeline.run("video/watch")
    assert timeline.scores["#vshown"] == 0
    timeline.dimension = "minecraft:overworld"
    timeline.run("video/watch")
    assert timeline.scores["#vshown"] == 2
    assert timeline.drawn[-1] == 2
    draws = len(timeline.drawn)
    # The pointer means the next audio tick; a paused image must not follow it.
    timeline.scores["#frame"] = 0
    timeline.run("video/watch")
    assert len(timeline.drawn) == draws
    assert timeline.scores["#vwanted"] == 2


def test_pixels_are_square_contiguous_and_live_at_one_chunk_origin(tmp_path: Path) -> None:
    video = make_video()
    _metadata, functions = make_pack(tmp_path, video)
    summons = [line for line in functions["video/internal/spawn_pixels"] if line.startswith("summon")]
    assert len(summons) == 4
    rectangles = []
    for command in summons:
        assert command.startswith("summon minecraft:text_display ~ ~ ~ ")
        assert 'billboard:"fixed"' in command
        assert '"font":"minecraft:default"' in command
        assert "brightness:{block:15,sky:15}" in command
        translation = re.search(r"translation:\[([^]]+)\]", command)[1]
        scale = re.search(r"scale:\[([^]]+)\]", command)[1]
        tx, ty, _ = [float(value.rstrip("f")) for value in translation.split(",")]
        sx, sy, _ = [float(value.rstrip("f")) for value in scale.split(",")]
        # Vanilla's one-space background occupies these font-space bounds.
        rectangles.append((tx - .05 * sx, tx + .075 * sx, ty, ty + .25 * sy))
    for left, right, bottom, top in rectangles:
        assert right - left == pytest.approx(.25)
        assert top - bottom == pytest.approx(.25)
    assert rectangles[0][1] == pytest.approx(rectangles[1][0])
    assert rectangles[0][2] == pytest.approx(rectangles[2][3])
    assert 'run tp @s ~ ~ ~ ~180 0' in functions["video/internal/spawn_pixels"][-2]


def test_screen_lifecycle_is_scoped_and_cleans_unloaded_old_generations(tmp_path: Path) -> None:
    video = make_video()
    metadata, functions = make_pack(tmp_path, video)
    other, _ = make_pack(tmp_path, video, namespace="other")
    assert metadata["video"]["entity_tag"] != other["video"]["entity_tag"]
    assert " at @s run " in functions["video/screen_create"][0]
    assert functions["tick"][0] == "function video_song:video/clean"
    cleanup = "\n".join(functions["video/clean"])
    removal = "\n".join(functions["video/screen_remove"])
    for dimension in VIDEO_DIMENSIONS:
        assert f"execute in {dimension}" in cleanup
        assert f"execute in {dimension}" in removal
    assert "unless score @s" in cleanup and "= #vsession" in cleanup
    assert "run kill @s" in cleanup
    assert "scoreboard players add #vsession" in removal
    assert f"kill @e[tag={metadata['video']['entity_tag']}]" in removal
    assert "#playing" not in removal and "#frame" not in removal
    assert 'positioned ^ ^1.6 ^3' in functions["video/internal/create"][1]


def test_small_pixel_sizes_keep_nonzero_display_scales(tmp_path: Path) -> None:
    video = make_video()
    video = VideoFrames(VideoConfig(2, 2, 10, 1e-9), video.info, video.frames)
    _metadata, functions = make_pack(tmp_path, video)
    command = functions["video/internal/spawn_pixels"][0]
    scale = re.search(r"scale:\[([^]]+)\]", command)[1]
    sx, sy, _ = [float(value.rstrip("f")) for value in scale.split(",")]
    assert sx == pytest.approx(8e-9, rel=1e-6, abs=0)
    assert sy == pytest.approx(4e-9, rel=1e-6, abs=0)


@pytest.mark.parametrize("pack_format,encoding", [(12, "json"), (61, "json"), (71, "snbt"), (121, "snbt")])
def test_display_text_encoding_tracks_component_migration(
    tmp_path: Path, pack_format: float, encoding: str,
) -> None:
    video = make_video()
    metadata, functions = make_pack(tmp_path, video, pack_format=pack_format)
    command = functions["video/internal/spawn_pixels"][0]
    assert metadata["video"]["text_component_encoding"] == encoding
    if encoding == "json":
        assert 'text:"{\\"text\\":' in command
    else:
        assert 'text:{"text":' in command
    with pytest.raises(ValueError, match="1.19.4"):
        video_metadata("song", video, 11)


def test_video_generation_progress_and_cancellation_preserve_existing_outputs(tmp_path: Path) -> None:
    video = make_video()
    updates = []
    target = tmp_path / "song.zip"
    frames = [AudioFrame(index, ()) for index in range(video.tick_count)]
    build_data_pack(target, frames, "song", "wav2mc", 121, "modern", video=video, progress_callback=updates.append)
    fractions = [update.fraction for update in updates]
    assert fractions == sorted(fractions)
    assert fractions[-1] == 1
    target.write_bytes(b"previous result")
    cancel = False

    def progress(update: object) -> None:
        nonlocal cancel
        if "Writing video frame" in update.detail:
            cancel = True

    with pytest.raises(TaskCancelled):
        build_data_pack(
            target, frames, "song", "wav2mc", 121, "modern", video=video,
            progress_callback=progress, cancel_check=lambda: cancel,
        )
    assert target.read_bytes() == b"previous result"
    assert not list(tmp_path.glob(".wav2mc-datapack-*"))


def test_invalid_video_frames_and_short_timeline_are_rejected_before_output(tmp_path: Path) -> None:
    video = make_video()
    target = tmp_path / "song.zip"
    with pytest.raises(ValueError, match="timeline"):
        build_data_pack(target, [AudioFrame(0, ())], "song", "wav2mc", 121, "modern", video=video)
    assert not target.exists()
    malformed = VideoFrames(video.config, video.info, video.frames.astype(np.float32))
    with pytest.raises(ValueError, match="uint8 RGB"):
        video_metadata("song", malformed, 121)
