from __future__ import annotations

import json
import math
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator

import numpy as np

from .config import VideoConfig
from .utils import (
    CancelCheck,
    ProgressCallback,
    check_cancelled,
    emit_progress,
    ensure_command,
    temporary_directory,
)


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    duration: float | None
    video_stream: int
    stream_index: int
    codec: str
    rotation: int = 0
    sample_aspect_ratio: float = 1.0

    @property
    def display_width(self) -> int:
        return self.height if self.rotation % 180 == 90 else self.width

    @property
    def display_height(self) -> int:
        return self.width if self.rotation % 180 == 90 else self.height

    @property
    def display_pixel_aspect_ratio(self) -> float:
        return 1.0 / self.sample_aspect_ratio if self.rotation % 180 == 90 else self.sample_aspect_ratio


@dataclass(frozen=True)
class VideoFrames:
    config: VideoConfig
    info: VideoInfo
    frames: np.ndarray

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def tick_count(self) -> int:
        return self.frame_count * self.config.ticks_per_frame

    @property
    def duration_seconds(self) -> float:
        return self.frame_count / self.config.fps


def _duration(value: object) -> float | None:
    try:
        duration = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return duration if math.isfinite(duration) and duration >= 0.0 else None


def _last_error(stderr: str, fallback: str) -> str:
    lines = stderr.strip().splitlines()
    return lines[-1] if lines else fallback


def _sample_aspect_ratio(value: object) -> float:
    try:
        numerator, denominator = str(value).split(":")
        ratio = float(numerator) / float(denominator)
    except (ValueError, TypeError, ZeroDivisionError):
        return 1.0
    return ratio if math.isfinite(ratio) and ratio > 0 else 1.0


def _rotation(stream: dict) -> int:
    for side_data in stream.get("side_data_list") or ():
        if isinstance(side_data, dict) and "rotation" in side_data:
            value = side_data["rotation"]
            break
    else:
        tags = stream.get("tags") or {}
        value = tags.get("rotate", 0) if isinstance(tags, dict) else 0
    try:
        angle = float(value)
    except (TypeError, ValueError):
        return 0
    return round(angle) % 360 if math.isfinite(angle) else 0


def resolve_video_config(config: VideoConfig, info: VideoInfo) -> VideoConfig:
    """Resolve source-native dimensions after the stream's display rotation."""
    config.validate()
    if not config.native_resolution:
        return config
    if info.rotation % 90:
        raise ValueError("Native video requires a display rotation in multiples of 90 degrees")
    resolved = replace(
        config,
        width=info.display_width,
        height=info.display_height,
        pixel_size=3.2 / info.display_width if config.pixel_size is None else config.pixel_size,
        pixel_aspect_ratio=info.display_pixel_aspect_ratio,
    )
    resolved.validate()
    return resolved


def probe_video(source: Path, video_stream: int = 0) -> VideoInfo:
    """Describe one zero-based video stream; audio and attached covers are rejected."""
    if (
        not isinstance(video_stream, int) or isinstance(video_stream, bool)
        or video_stream < 0
    ):
        raise ValueError("video_stream must be a nonnegative integer")
    if not source.is_file():
        raise FileNotFoundError(source)
    try:
        result = subprocess.run(
            [
                ensure_command("ffprobe"), "-v", "error", "-select_streams",
                f"v:{video_stream}", "-show_entries",
                "format=duration:stream=index,codec_type,codec_name,width,height,duration,sample_aspect_ratio:"
                "stream_disposition=attached_pic:stream_side_data=rotation:stream_tags=rotate",
                "-of", "json", str(source),
            ],
            check=False, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"FFprobe timed out reading video from {source}") from exc
    if result.returncode:
        reason = _last_error(result.stderr, "unknown probe error")
        raise RuntimeError(f"FFprobe could not read video from {source}: {reason}")
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("not an object")
        streams = payload.get("streams") or []
        if not isinstance(streams, list):
            raise ValueError("invalid streams")
        if not streams:
            raise RuntimeError(f"No video stream {video_stream} found in {source}")
        stream = streams[0]
        if not isinstance(stream, dict) or stream.get("codec_type") != "video":
            raise ValueError("invalid video stream")
        disposition = stream.get("disposition") or {}
        if isinstance(disposition, dict) and disposition.get("attached_pic"):
            raise RuntimeError(f"Video stream {video_stream} is an attached cover image")
        width, height = int(stream["width"]), int(stream["height"])
        stream_index = int(stream["index"])
        if width <= 0 or height <= 0 or stream_index < 0:
            raise ValueError("invalid stream dimensions")
        format_data = payload.get("format") or {}
        format_duration = (
            _duration(format_data.get("duration"))
            if isinstance(format_data, dict) else None
        )
        duration = _duration(stream.get("duration"))
        return VideoInfo(
            width, height, duration if duration is not None else format_duration,
            video_stream, stream_index, str(stream.get("codec_name") or "unknown"),
            _rotation(stream),
            _sample_aspect_ratio(stream.get("sample_aspect_ratio")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("FFprobe returned an invalid video description") from exc


def _stop_decoder(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    else:
        process.wait()


@contextmanager
def open_video_frames(
    source: Path,
    config: VideoConfig,
    video_stream: int = 0,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> Iterator[VideoFrames]:
    """Decode RGB frames to a temporary memory map, valid inside this context only.

    The decoded video stays on disk, so long clips do not require keeping every
    frame in RAM. FFmpeg's stderr also goes to disk instead of an undrained pipe.
    """
    config.validate()
    check_cancelled(cancel_check)
    info = probe_video(source, video_stream)
    config = resolve_video_config(config, info)
    check_cancelled(cancel_check)
    emit_progress(progress_callback, "video_decode", 0.0, "Decoding video")
    frame_bytes = config.width * config.height * 3
    with temporary_directory("wav2mc-video-") as temporary:
        raw_path = temporary / "frames.rgb"
        stderr_path = temporary / "ffmpeg.log"
        video_filter = "setsar=1," if config.native_resolution else (
            f"scale=w='max(1,min({config.width},round({config.height}*dar)))':"
            f"h='max(1,min({config.height},round({config.width}/dar)))',setsar=1,"
            f"pad={config.width}:{config.height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        )
        video_filter += f"fps={config.fps}:start_time=0:eof_action=pass"
        command = [
            ensure_command("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
            "-y", "-i", str(source), "-map", f"0:{info.stream_index}", "-an", "-sn",
            "-dn", "-vf", video_filter, "-filter_threads", "1", "-pix_fmt", "rgb24",
            "-threads", "1", "-f", "rawvideo", str(raw_path),
        ]
        with stderr_path.open("wb") as error_file:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=error_file,
            )
            last_fraction = 0.0
            try:
                while process.poll() is None:
                    check_cancelled(cancel_check)
                    size = raw_path.stat().st_size if raw_path.exists() else 0
                    decoded_frames = size // frame_bytes
                    if info.duration and decoded_frames:
                        fraction = min(0.99, decoded_frames / (info.duration * config.fps))
                        if fraction > last_fraction:
                            emit_progress(
                                progress_callback, "video_decode", fraction,
                                f"Decoding video: {decoded_frames} frames",
                            )
                            last_fraction = fraction
                    time.sleep(0.05)
                process.wait()
                check_cancelled(cancel_check)
            except BaseException:
                _stop_decoder(process)
                raise
        if process.returncode:
            with stderr_path.open("rb") as error_file:
                error_file.seek(0, 2)
                error_file.seek(max(0, error_file.tell() - 4096))
                reason = _last_error(
                    error_file.read().decode("utf-8", errors="replace"),
                    "unknown decoder error",
                )
            raise RuntimeError(f"FFmpeg could not decode video from {source}: {reason}")
        size = raw_path.stat().st_size if raw_path.exists() else 0
        if size % frame_bytes:
            raise RuntimeError("FFmpeg produced an incomplete RGB video frame")
        frame_count = size // frame_bytes
        if frame_count == 0:
            raise RuntimeError("FFmpeg produced no video frames")
        frames = np.memmap(
            raw_path, mode="r", dtype=np.uint8,
            shape=(frame_count, config.height, config.width, 3),
        )
        try:
            emit_progress(progress_callback, "video_decode", 1.0, "Video decoded")
            check_cancelled(cancel_check)
            yield VideoFrames(config, info, frames)
        finally:
            frames._mmap.close()


def decode_video(
    source: Path,
    config: VideoConfig,
    video_stream: int = 0,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> VideoFrames:
    """Decode a short video into RAM; use open_video_frames for conversion jobs."""
    with open_video_frames(
        source, config, video_stream, progress_callback, cancel_check,
    ) as decoded:
        return VideoFrames(decoded.config, decoded.info, np.array(decoded.frames))
