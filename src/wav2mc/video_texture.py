from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import BinaryIO

import numpy as np

from .config import VIDEO_TILE_SIZE, VideoConfig
from .utils import (
    CancelCheck,
    ProgressCallback,
    check_cancelled,
    emit_progress,
    pack_metadata,
    scaled_progress,
    temporary_directory,
    write_json,
    zip_directory,
)
from .video import VideoFrames


_FIRST_GLYPH = 0xF0000
_LAST_GLYPH = 0xFFFFD


def texture_layout(config: VideoConfig) -> dict[str, int]:
    """Describe bitmap-font cells without imposing a per-pixel entity limit."""
    config.validate()
    if config.width is None or config.height is None:
        raise ValueError("Video dimensions must be resolved before creating textures")
    columns = (config.width + VIDEO_TILE_SIZE - 1) // VIDEO_TILE_SIZE
    rows = (config.height + VIDEO_TILE_SIZE - 1) // VIDEO_TILE_SIZE
    count = columns * rows
    if count > _LAST_GLYPH - _FIRST_GLYPH + 1:
        raise ValueError("Video requires more bitmap-font tiles than available glyphs")
    return {
        "tile_size": VIDEO_TILE_SIZE,
        "tile_columns": columns,
        "tile_rows": rows,
        "display_entities": count,
        "atlas_width": columns * VIDEO_TILE_SIZE,
        "atlas_height": rows * VIDEO_TILE_SIZE,
    }


def video_font_id(namespace: str, frame_index: int) -> str:
    return f"{namespace}:video/frame_{frame_index:06d}"


def tile_glyph(index: int) -> str:
    codepoint = _FIRST_GLYPH + index
    if not _FIRST_GLYPH <= codepoint <= _LAST_GLYPH:
        raise ValueError("Video tile index exceeds the private-use glyph range")
    return chr(codepoint)


def tile_text_component(namespace: str, frame_index: int, tile: int) -> dict[str, object]:
    return {
        "text": tile_glyph(tile),
        "font": video_font_id(namespace, frame_index),
        "color": "white",
        "bold": False,
        "italic": False,
    }


def _write_chunk(output: BinaryIO, kind: bytes, data: bytes) -> None:
    output.write(struct.pack(">I", len(data)))
    output.write(kind)
    output.write(data)
    output.write(struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def write_frame_png(
    path: Path, frame: np.ndarray, cancel_check: CancelCheck | None = None,
) -> None:
    """Write exact RGB pixels with transparent cell padding, one row at a time."""
    if frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("Texture frames must be uint8 RGB arrays")
    height, width = frame.shape[:2]
    if not height or not width:
        raise ValueError("Texture frames cannot be empty")
    atlas_width = ((width + VIDEO_TILE_SIZE - 1) // VIDEO_TILE_SIZE) * VIDEO_TILE_SIZE
    atlas_height = ((height + VIDEO_TILE_SIZE - 1) // VIDEO_TILE_SIZE) * VIDEO_TILE_SIZE
    path.parent.mkdir(parents=True, exist_ok=True)
    compressor = zlib.compressobj(level=6)
    row = np.zeros((atlas_width, 4), dtype=np.uint8)
    with path.open("wb") as output:
        output.write(b"\x89PNG\r\n\x1a\n")
        _write_chunk(output, b"IHDR", struct.pack(">IIBBBBB", atlas_width, atlas_height, 8, 6, 0, 0, 0))
        for y in range(atlas_height):
            if y % 32 == 0:
                check_cancelled(cancel_check)
            if y < height:
                row[:width, :3] = frame[y]
                row[:width, 3] = 255
            elif y == height:
                row.fill(0)
            compressed = compressor.compress(b"\x00" + row.tobytes())
            if compressed:
                _write_chunk(output, b"IDAT", compressed)
        compressed = compressor.flush()
        if compressed:
            _write_chunk(output, b"IDAT", compressed)
        _write_chunk(output, b"IEND", b"")


def write_video_resource_pack(
    output: Path,
    namespace: str,
    video: VideoFrames,
    pack_format: float,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    data_pack_format: float = 121.0,
) -> None:
    """Create a companion pack of unscaled video frames and colored bitmap fonts."""
    # Local import avoids coupling the renderer's module initialization to assets.
    from .video_datapack import video_metadata

    metadata = video_metadata(namespace, video, data_pack_format)
    if metadata["renderer"] != "text_display_texture":
        raise ValueError("A video texture resource pack requires the texture renderer")
    layout = texture_layout(video.config)
    rows, columns = layout["tile_rows"], layout["tile_columns"]
    chars = [
        "".join(tile_glyph(y * columns + x) for x in range(columns))
        for y in range(rows)
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    with temporary_directory(".wav2mc-video-pack-", directory=output.parent) as staging:
        root = staging / "content"
        staged_output = staging / output.name
        write_json(root / "pack.mcmeta", {
            "pack": pack_metadata(pack_format, f"wav2mc video textures: {namespace}"),
        })
        write_json(root / "wav2mc-video.json", {
            "namespace": namespace,
            "video": metadata,
        })
        for index, frame in enumerate(video.frames):
            check_cancelled(cancel_check)
            emit_progress(
                progress_callback, "video_resource_pack", 0.84 * index / video.frame_count,
                f"Writing video texture {index + 1}/{video.frame_count}",
            )
            stem = f"frame_{index:06d}"
            write_frame_png(
                root / "assets" / namespace / "textures" / "video" / f"{stem}.png",
                frame, cancel_check,
            )
            write_json(root / "assets" / namespace / "font" / "video" / f"{stem}.json", {
                "providers": [{
                    "type": "bitmap",
                    "file": f"{namespace}:video/{stem}.png",
                    "height": 8,
                    "ascent": 7,
                    "chars": chars,
                }],
            })
        zip_directory(
            root, staged_output,
            progress_callback=scaled_progress(progress_callback, 0.84, 0.99, "video_resource_pack"),
            cancel_check=cancel_check,
        )
        check_cancelled(cancel_check)
        staged_output.replace(output)
        emit_progress(progress_callback, "video_resource_pack", 1.0, "Video resource pack complete")
