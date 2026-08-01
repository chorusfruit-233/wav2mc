from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from .utils import ensure_command


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
) -> None:
    """Decode any FFmpeg-supported input into mono PCM WAV."""
    if audio_stream < 0:
        raise ValueError("audio_stream must not be negative")
    ffmpeg = ensure_command("ffmpeg")
    target.parent.mkdir(parents=True, exist_ok=True)
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
        "1",
        "-ar",
        str(sample_rate),
        "-af",
        audio_filter,
        "-c:a",
        "pcm_s16le",
        str(target),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()
        reason = detail[-1] if detail else "unknown decoder error"
        raise RuntimeError(
            f"FFmpeg could not decode an audio stream from {source}: {reason}"
        ) from exc


def load_mono(path: Path, expected_sample_rate: int) -> np.ndarray:
    audio, sample_rate = sf.read(path, dtype="float32", always_2d=False)
    if sample_rate != expected_sample_rate:
        raise ValueError(
            f"Expected {expected_sample_rate} Hz after preprocessing, got {sample_rate} Hz"
        )
    if audio.ndim == 2:
        audio = audio.mean(axis=1)
    audio = np.asarray(audio, dtype=np.float32)
    if not np.all(np.isfinite(audio)):
        raise ValueError("Input contains NaN or infinite samples")
    return audio


def peak_normalize(audio: np.ndarray, target_peak: float = 0.92) -> np.ndarray:
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak <= 1e-12:
        return audio.copy()
    return (audio * (target_peak / peak)).astype(np.float32)


def write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, np.asarray(audio, dtype=np.float32), sample_rate, subtype="PCM_16")
