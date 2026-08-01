from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from .audio import sqrt_hann
from .config import AudioConfig
from .utils import temporary_directory, write_json, zip_directory


def sound_event_name(frequency: int, phase_index: int) -> str:
    return f"grain.f{frequency:04d}.p{phase_index:02d}"


def build_resource_pack(
    output: Path,
    config: AudioConfig,
    pack_format: int,
    namespace: str = "wav2mc",
    grain_level: float = 1.0,
) -> None:
    if not 0.0 < grain_level <= 1.0:
        raise ValueError("grain_level must be in (0, 1]")

    n = config.window_size
    window = sqrt_hann(n)
    positions = np.arange(n, dtype=np.float64) / config.sample_rate

    with temporary_directory("wav2mc-bank-") as root:
        write_json(
            root / "pack.mcmeta",
            {
                "pack": {
                    "pack_format": pack_format,
                    "description": "wav2mc reusable sine-grain bank",
                }
            },
        )
        write_json(
            root / "wav2mc-bank.json",
            {
                "namespace": namespace,
                "sample_rate": config.sample_rate,
                "grain_ms": config.grain_ms,
                "hop_ms": config.hop_ms,
                "min_frequency": config.min_frequency,
                "max_frequency": config.max_frequency,
                "frequency_step": config.frequency_step,
                "phase_count": config.phase_count,
                "grain_level": grain_level,
            },
        )

        sounds: dict[str, object] = {}
        sound_root = root / "assets" / namespace / "sounds" / "grain"

        for frequency in config.frequencies:
            frequency_dir = sound_root / f"f{frequency:04d}"
            frequency_dir.mkdir(parents=True, exist_ok=True)
            for phase_index in range(config.phase_count):
                phase = 2.0 * np.pi * phase_index / config.phase_count
                audio = grain_level * window * np.cos(
                    2.0 * np.pi * frequency * positions + phase
                )
                file_path = frequency_dir / f"p{phase_index:02d}.ogg"
                sf.write(
                    file_path,
                    audio.astype(np.float32),
                    config.sample_rate,
                    format="OGG",
                    subtype="VORBIS",
                )

                event = sound_event_name(frequency, phase_index)
                sounds[event] = {
                    "sounds": [
                        {
                            "name": (
                                f"{namespace}:grain/f{frequency:04d}/p{phase_index:02d}"
                            ),
                            "stream": False,
                        }
                    ]
                }

        write_json(root / "assets" / namespace / "sounds.json", sounds)
        zip_directory(root, output)
