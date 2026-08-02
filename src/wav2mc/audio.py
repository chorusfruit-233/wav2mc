from __future__ import annotations

import json
import queue
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .utils import (
    CancelCheck,
    ProgressCallback,
    TaskCancelled,
    check_cancelled,
    emit_progress,
    ensure_command,
)


@dataclass(frozen=True)
class AudioStreamInfo:
    audio_index: int
    codec: str
    sample_rate: int
    channels: int
    duration: float | None


@dataclass(frozen=True)
class MediaInfo:
    duration: float | None
    format_name: str
    streams: tuple[AudioStreamInfo, ...]


def _optional_float(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) and result >= 0.0 else None


def probe_media(source: Path) -> MediaInfo:
    if not source.is_file():
        raise FileNotFoundError(source)
    ffprobe = ensure_command("ffprobe")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration,format_name:"
            "stream=codec_type,codec_name,sample_rate,channels,duration",
            "-of",
            "json",
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "FFprobe could not read media")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("FFprobe returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("FFprobe returned an invalid media description")

    format_data = payload.get("format") or {}
    if not isinstance(format_data, dict):
        format_data = {}
    format_duration = _optional_float(format_data.get("duration"))
    streams = []
    for stream in payload.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        if stream.get("codec_type") != "audio":
            continue
        streams.append(
            AudioStreamInfo(
                audio_index=len(streams),
                codec=str(stream.get("codec_name") or "unknown"),
                sample_rate=_optional_int(stream.get("sample_rate")),
                channels=_optional_int(stream.get("channels")),
                duration=_optional_float(stream.get("duration")) or format_duration,
            )
        )
    if not streams:
        raise RuntimeError(f"No audio stream found in {source}")
    stream_duration = max(
        (stream.duration for stream in streams if stream.duration is not None),
        default=None,
    )
    return MediaInfo(
        duration=format_duration if format_duration is not None else stream_duration,
        format_name=str(format_data.get("format_name") or "unknown"),
        streams=tuple(streams),
    )


def _optional_int(value: object) -> int:
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, result)


def sqrt_hann(window_size: int) -> np.ndarray:
    """Return a symmetric square-root Hann window with zero endpoints."""
    if window_size < 2:
        raise ValueError("window_size must be at least 2")
    return np.sqrt(np.hanning(window_size)).astype(np.float32)


def preprocess_audio(
    source: Path,
    target: Path,
    sample_rate: int,
    low_frequency: int,
    high_frequency: int,
    audio_stream: int = 0,
    channels: int = 1,
    duration_seconds: float | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> None:
    """Decode any FFmpeg-supported input into mono or stereo PCM WAV."""
    if audio_stream < 0:
        raise ValueError("audio_stream must not be negative")
    if channels not in (1, 2):
        raise ValueError("channels must be 1 or 2")
    if not source.is_file():
        raise FileNotFoundError(source)
    ffmpeg = ensure_command("ffmpeg")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{target.stem}.",
        suffix=".wav",
        dir=target.parent,
        delete=False,
    ) as temporary:
        staged_target = Path(temporary.name)
    audio_filter = f"highpass=f={low_frequency},lowpass=f={high_frequency}"
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-map",
        f"0:a:{audio_stream}",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
        "-af",
        audio_filter,
        "-progress",
        "pipe:1",
        "-nostats",
        "-c:a",
        "pcm_s16le",
        str(staged_target),
    ]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except Exception:
        staged_target.unlink(missing_ok=True)
        raise
    progress_lines: queue.Queue[str] = queue.Queue()
    error_lines: list[str] = []

    def read_progress() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            progress_lines.put(line.rstrip())

    def read_errors() -> None:
        assert process.stderr is not None
        error_lines.extend(process.stderr)

    progress_thread = threading.Thread(target=read_progress, daemon=True)
    error_thread = threading.Thread(target=read_errors, daemon=True)
    progress_thread.start()
    error_thread.start()
    emit_progress(progress_callback, "decode", 0.0, "Decoding audio")
    try:
        while process.poll() is None:
            check_cancelled(cancel_check)
            while True:
                try:
                    line = progress_lines.get_nowait()
                except queue.Empty:
                    break
                key, separator, value = line.partition("=")
                if separator and key == "out_time_us" and duration_seconds:
                    try:
                        decoded_seconds = int(value) / 1_000_000.0
                    except ValueError:
                        continue
                    emit_progress(
                        progress_callback,
                        "decode",
                        decoded_seconds / duration_seconds,
                        "Decoding audio",
                    )
            time.sleep(0.05)
    except TaskCancelled:
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        staged_target.unlink(missing_ok=True)
        raise
    except Exception:
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        staged_target.unlink(missing_ok=True)
        raise

    progress_thread.join(timeout=1.0)
    error_thread.join(timeout=1.0)
    if process.returncode != 0:
        staged_target.unlink(missing_ok=True)
        detail = "".join(error_lines).strip().splitlines()
        reason = detail[-1] if detail else "unknown decoder error"
        raise RuntimeError(
            f"FFmpeg could not decode an audio stream from {source}: {reason}"
        )
    try:
        check_cancelled(cancel_check)
    except TaskCancelled:
        staged_target.unlink(missing_ok=True)
        raise
    try:
        staged_target.replace(target)
    except OSError:
        staged_target.unlink(missing_ok=True)
        raise
    emit_progress(progress_callback, "decode", 1.0, "Audio decoded")


def load_audio(path: Path, expected_sample_rate: int) -> np.ndarray:
    audio, sample_rate = sf.read(path, dtype="float32", always_2d=True)
    if sample_rate != expected_sample_rate:
        raise ValueError(
            f"Expected {expected_sample_rate} Hz after preprocessing, got {sample_rate} Hz"
        )
    audio = np.asarray(audio, dtype=np.float32)
    if not np.all(np.isfinite(audio)):
        raise ValueError("Input contains NaN or infinite samples")
    if audio.shape[1] == 1:
        return audio[:, 0]
    if audio.shape[1] != 2:
        raise ValueError("Decoded audio must contain one or two channels")
    if np.array_equal(audio[:, 0], audio[:, 1]):
        return audio[:, 0]
    return audio


def load_mono(path: Path, expected_sample_rate: int) -> np.ndarray:
    audio = load_audio(path, expected_sample_rate)
    return audio.mean(axis=1) if audio.ndim == 2 else audio


def peak_normalize(audio: np.ndarray, target_peak: float = 0.92) -> np.ndarray:
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak <= 1e-12:
        return audio.copy()
    return (audio * (target_peak / peak)).astype(np.float32)


def write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, np.asarray(audio, dtype=np.float32), sample_rate, subtype="PCM_16")
