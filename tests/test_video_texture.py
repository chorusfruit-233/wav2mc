from __future__ import annotations

import json
import math
import re
import struct
import zlib
from dataclasses import replace
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pytest

from wav2mc.analysis import AudioFrame
from wav2mc.config import VideoConfig
from wav2mc.datapack import build_data_pack
from wav2mc.utils import TaskCancelled
from wav2mc.video import VideoFrames, VideoInfo
from wav2mc.video_datapack import video_metadata
from wav2mc.video_texture import (
    VIDEO_TILE_SIZE,
    texture_layout,
    tile_glyph,
    write_frame_png,
    write_video_resource_pack,
)


def make_video(width: int = 259, height: int = 258, count: int = 2) -> VideoFrames:
    pixels = np.arange(count * height * width * 3, dtype=np.uint8).reshape(count, height, width, 3)
    return VideoFrames(
        VideoConfig(width, height, 10, 3.2 / width, renderer="texture"),
        VideoInfo(width, height, count / 10, 0, 0, "rawvideo"), pixels,
    )


def read_png(data: bytes) -> np.ndarray:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    compressed = []
    width = height = 0
    while offset < len(data):
        size, kind = struct.unpack(">I4s", data[offset:offset + 8])
        content = data[offset + 8:offset + 8 + size]
        crc = struct.unpack(">I", data[offset + 8 + size:offset + 12 + size])[0]
        assert crc == zlib.crc32(kind + content) & 0xFFFFFFFF
        if kind == b"IHDR":
            width, height, depth, color, *_rest = struct.unpack(">IIBBBBB", content)
            assert (depth, color) == (8, 6)
        elif kind == b"IDAT":
            compressed.append(content)
        offset += size + 12
    raw = np.frombuffer(zlib.decompress(b"".join(compressed)), dtype=np.uint8)
    rows = raw.reshape(height, width * 4 + 1)
    assert not np.any(rows[:, 0])
    return rows[:, 1:].reshape(height, width, 4)


def test_texture_png_keeps_every_rgb_pixel_and_transparently_pads_edges(tmp_path: Path) -> None:
    video = make_video()
    path = tmp_path / "frame.png"
    write_frame_png(path, video.frames[0])
    actual = read_png(path.read_bytes())
    assert actual.shape == (512, 512, 4)
    np.testing.assert_array_equal(actual[:258, :259, :3], video.frames[0])
    assert np.all(actual[:258, :259, 3] == 255)
    assert not np.any(actual[258:, :, 3])
    assert not np.any(actual[:, 259:, 3])


def test_native_resolution_uses_tiles_instead_of_millions_of_entities(tmp_path: Path) -> None:
    frame = np.zeros((1, 1080, 1920, 3), dtype=np.uint8)
    video = VideoFrames(
        VideoConfig(1920, 1080, 20, 3.2 / 1920, native_resolution=True),
        VideoInfo(1920, 1080, 0.05, 0, 0, "rawvideo"), frame,
    )
    metadata = video_metadata("native", video, 121)
    assert metadata["renderer"] == "text_display_texture"
    assert metadata["quality"] == "native"
    assert metadata["native_resolution"] is True
    assert metadata["pixel_entities"] == 0
    assert metadata["display_entities"] == 40
    assert metadata["entity_count"] == 41
    assert metadata["display_updates_per_second"] == 800
    assert metadata["estimated_texture_bytes"] == 40 * 256 * 256 * 4
    target = tmp_path / "native.zip"
    build_data_pack(target, [AudioFrame(0, ())], "native", "wav2mc", 121, "modern", video=video)
    with ZipFile(target) as archive:
        spawn = archive.read("data/native/function/video/internal/spawn_pixels.mcfunction").decode()
        assert spawn.count("summon minecraft:text_display") == 40
        updates = archive.read("data/native/function/video/frame/000000.mcfunction").decode().splitlines()
        assert len(updates) == 40
        assert all("{text:" in line and "background:" not in line for line in updates)


@pytest.mark.parametrize("pack_format", [61, 121])
def test_texture_pack_font_grid_and_frame_commands_reference_the_same_pixels(
    tmp_path: Path, pack_format: float,
) -> None:
    video = make_video()
    assets = tmp_path / "textures.zip"
    updates = []
    write_video_resource_pack(
        assets, "song", video, 97.1,
        progress_callback=updates.append, data_pack_format=pack_format,
    )
    with ZipFile(assets) as archive:
        assert archive.testzip() is None
        metadata = json.loads(archive.read("wav2mc-video.json"))
        assert metadata["video"] == video_metadata("song", video, pack_format)
        for index in range(video.frame_count):
            provider = json.loads(archive.read(f"assets/song/font/video/frame_{index:06d}.json"))["providers"][0]
            assert provider["type"] == "bitmap"
            assert provider["height"] == 8 and provider["ascent"] == 7
            assert provider["chars"] == [tile_glyph(0) + tile_glyph(1), tile_glyph(2) + tile_glyph(3)]
            pixels = read_png(archive.read(f"assets/song/textures/video/frame_{index:06d}.png"))
            np.testing.assert_array_equal(pixels[:258, :259, :3], video.frames[index])
    target = tmp_path / "data.zip"
    build_data_pack(
        target, [AudioFrame(index, ()) for index in range(video.tick_count)],
        "song", "wav2mc", pack_format, "modern", video=video,
    )
    with ZipFile(target) as archive:
        commands = archive.read("data/song/function/video/frame/000001.mcfunction").decode().splitlines()
        for tile, command in enumerate(commands):
            assert "song:video/frame_000001" in command
            assert tile_glyph(tile) in command
            if pack_format < 71:
                assert '{text:"{' in command
            else:
                assert '{text:{' in command
    fractions = [update.fraction for update in updates]
    assert fractions == sorted(fractions)
    assert fractions[-1] == 1.0


def test_texture_tile_geometry_preserves_pixel_positions_and_cropped_edges(tmp_path: Path) -> None:
    video = make_video(count=1)
    target = tmp_path / "data.zip"
    build_data_pack(
        target, [AudioFrame(index, ()) for index in range(video.tick_count)],
        "song", "wav2mc", 121, "modern", video=video,
    )
    with ZipFile(target) as archive:
        commands = archive.read("data/song/function/video/internal/spawn_pixels.mcfunction").decode().splitlines()
    summons = [line for line in commands if line.startswith("summon")]
    p = video.config.pixel_size
    for tile, command in enumerate(summons):
        x, y = tile % 2, tile // 2
        assert command.startswith("summon minecraft:text_display ~ ~ ~ ")
        assert 'background:0,' in command and 'alignment:"left"' in command
        assert '"color":"white"' in command and '"italic":false' in command
        translation = re.search(r"translation:\[([^]]+)\]", command)[1]
        scale = re.search(r"scale:\[([^]]+)\]", command)[1]
        tx, ty, _ = map(lambda value: float(value.rstrip("f")), translation.split(","))
        sx, sy, _ = map(lambda value: float(value.rstrip("f")), scale.split(","))
        w = min(256, video.config.width - x * 256)
        h = min(256, video.config.height - y * 256)
        advance = math.floor(0.5 + w / 32) + 1
        # Font height8/ascent7 renders an8×8quad; transparent padding crops it.
        left = tx + (1 - advance / 2) * 0.025 * sx
        top = ty + 9 * 0.025 * sy
        assert left == pytest.approx((-video.config.width / 2 + x * 256) * p)
        assert top == pytest.approx((video.config.height / 2 - y * 256) * p)
        assert w / 32 * 0.025 * sx == pytest.approx(w * p)
        assert h / 32 * 0.025 * sy == pytest.approx(h * p)


def test_texture_pack_cancellation_is_atomic_and_cleans_temporary_files(tmp_path: Path) -> None:
    video = make_video()
    target = tmp_path / "video.zip"
    target.write_bytes(b"previous pack")
    cancelled = False

    def progress(update: object) -> None:
        nonlocal cancelled
        if "Writing video texture" in update.detail:
            cancelled = True

    with pytest.raises(TaskCancelled):
        write_video_resource_pack(
            target, "song", video, 97.1,
            progress_callback=progress, cancel_check=lambda: cancelled,
        )
    assert target.read_bytes() == b"previous pack"
    assert sorted(tmp_path.iterdir()) == [target]


def test_native_non_square_pixels_keep_full_raster_and_correct_display_aspect(tmp_path: Path) -> None:
    original = make_video(count=1)
    video = VideoFrames(
        replace(original.config, native_resolution=True, pixel_aspect_ratio=2.0),
        replace(original.info, sample_aspect_ratio=2.0), original.frames,
    )
    metadata = video_metadata("wide", video, 121)
    assert (metadata["width"], metadata["height"]) == (259, 258)
    assert metadata["screen_width_blocks"] == pytest.approx(3.2)
    assert metadata["screen_height_blocks"] == pytest.approx(258 * video.config.pixel_size / 2)
    assert metadata["pixel_height_blocks"] == pytest.approx(video.config.pixel_size / 2)
    assert metadata["pixel_aspect_ratio"] == 2.0
    target = tmp_path / "data.zip"
    build_data_pack(
        target, [AudioFrame(index, ()) for index in range(video.tick_count)],
        "wide", "wav2mc", 121, "modern", video=video,
    )
    with ZipFile(target) as archive:
        command = archive.read("data/wide/function/video/internal/spawn_pixels.mcfunction").decode().splitlines()[0]
    values = re.search(r"scale:\[([^]]+)\]", command)[1]
    sx, sy, _ = [float(value.rstrip("f")) for value in values.split(",")]
    assert sy == pytest.approx(sx / 2)
    translation = re.search(r"translation:\[([^]]+)\]", command)[1]
    tx, ty, _ = [float(value.rstrip("f")) for value in translation.split(",")]
    assert tx + (1 - 9 / 2) * .025 * sx == pytest.approx(-3.2 / 2)
    assert ty + 9 * .025 * sy == pytest.approx(metadata["screen_height_blocks"] / 2)
    assets = tmp_path / "video.zip"
    write_video_resource_pack(assets, "wide", video, 97.1)
    with ZipFile(assets) as archive:
        pixels = read_png(archive.read("assets/wide/textures/video/frame_000000.png"))
    np.testing.assert_array_equal(pixels[:258, :259, :3], video.frames[0])


def test_texture_layout_rejects_unresolved_and_pixel_only_requests(tmp_path: Path) -> None:
    assert texture_layout(VideoConfig(3840, 2160, 20, 0.01))["display_entities"] == 135
    assert VIDEO_TILE_SIZE == 256
    with pytest.raises(ValueError, match="resolved"):
        texture_layout(VideoConfig(None, None, 20, None, native_resolution=True))
    video = make_video(2, 2)
    video = VideoFrames(VideoConfig(2, 2), video.info, video.frames)
    with pytest.raises(ValueError, match="texture renderer"):
        write_video_resource_pack(tmp_path / "unused.zip", "song", video, 97.1)
    assert not (tmp_path / "unused.zip").exists()
