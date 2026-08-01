from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from .audio import sqrt_hann
from .config import (
    DEVICE_PROFILES,
    DEFAULT_DEVICE_PACK_PROFILES,
    DEFAULT_MINECRAFT_VERSION,
    AudioConfig,
    audio_config_metadata,
    device_audio_config,
)
from .grains import RESIDUAL_KINDS, residual_event_name, residual_grain
from .utils import (
    pack_metadata,
    safe_namespace,
    temporary_directory,
    write_json,
    zip_directory,
)


def sound_event_name(frequency: int, phase_index: int) -> str:
    return f"grain.f{frequency:04d}.p{phase_index:02d}"


def build_resource_pack(
    output: Path,
    config: AudioConfig,
    pack_format: float,
    namespace: str = "wav2mc",
    grain_level: float = 1.0,
    device_profile: str | None = None,
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
                "pack": pack_metadata(
                    pack_format,
                    "wav2mc reusable hybrid audio bank"
                    + (f" ({device_profile})" if device_profile else ""),
                )
            },
        )
        write_json(
            root / "wav2mc-bank.json",
            {
                "minecraft_version": DEFAULT_MINECRAFT_VERSION,
                "namespace": namespace,
                **audio_config_metadata(config),
                "grain_level": grain_level,
                "device_profile": device_profile,
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

        for band_index, low, high in config.residual_bands:
            for kind in RESIDUAL_KINDS:
                band_root = (
                    root
                    / "assets"
                    / namespace
                    / "sounds"
                    / kind
                    / f"b{band_index:02d}"
                )
                band_root.mkdir(parents=True, exist_ok=True)
                for variant in range(config.residual_variant_count):
                    audio = grain_level * residual_grain(
                        config.sample_rate,
                        config.window_size,
                        band_index,
                        low,
                        high,
                        variant,
                        kind,
                    )
                    file_path = band_root / f"v{variant:02d}.ogg"
                    sf.write(
                        file_path,
                        audio.astype(np.float32),
                        config.sample_rate,
                        format="OGG",
                        subtype="VORBIS",
                    )

                    event = residual_event_name(kind, band_index, variant)
                    sounds[event] = {
                        "sounds": [
                            {
                                "name": (
                                    f"{namespace}:{kind}/b{band_index:02d}/"
                                    f"v{variant:02d}"
                                ),
                                "stream": False,
                            }
                        ]
                    }

        write_json(root / "assets" / namespace / "sounds.json", sounds)
        zip_directory(root, output)


def build_device_pack_set(
    output_dir: Path,
    base_config: AudioConfig,
    pack_format: float,
    namespace_prefix: str = "wav2mc",
    grain_level: float = 1.0,
    profile_names: tuple[str, ...] = DEFAULT_DEVICE_PACK_PROFILES,
) -> dict[str, Path]:
    if not profile_names:
        raise ValueError("At least one device profile is required")

    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Path] = {}
    manifest_profiles: dict[str, object] = {}
    for profile_name in profile_names:
        try:
            profile = DEVICE_PROFILES[profile_name]
        except KeyError as exc:
            raise ValueError(f"Unknown device profile: {profile_name}") from exc

        config = device_audio_config(base_config, profile)
        namespace = safe_namespace(f"{namespace_prefix}_{profile.name}")
        target = output_dir / f"{namespace}_sine_bank.zip"
        build_resource_pack(
            output=target,
            config=config,
            pack_format=pack_format,
            namespace=namespace,
            grain_level=grain_level,
            device_profile=profile.name,
        )
        outputs[profile.name] = target
        manifest_profiles[profile.name] = {
            "file": target.name,
            "namespace": namespace,
            "quality": profile.quality_name,
            "audio_config": audio_config_metadata(config),
            "sound_count": (
                len(config.frequencies) * config.phase_count
                + len(config.residual_bands)
                * config.residual_variant_count
                * len(RESIDUAL_KINDS)
            ),
            "residual_sound_count": (
                len(config.residual_bands)
                * config.residual_variant_count
                * len(RESIDUAL_KINDS)
            ),
        }

    manifest = output_dir / "wav2mc-device-packs.json"
    write_json(
        manifest,
        {
            "minecraft_version": DEFAULT_MINECRAFT_VERSION,
            "pack_format": pack_format,
            "grain_level": grain_level,
            "profiles": manifest_profiles,
        },
    )
    outputs["manifest"] = manifest
    return outputs
