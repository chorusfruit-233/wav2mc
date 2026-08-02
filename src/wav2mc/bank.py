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
    CancelCheck,
    ProgressCallback,
    check_cancelled,
    emit_progress,
    pack_metadata,
    safe_namespace,
    scaled_progress,
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
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> None:
    if not 0.0 < grain_level <= 1.0:
        raise ValueError("grain_level must be in (0, 1]")

    n = config.window_size
    window = sqrt_hann(n)
    positions = np.arange(n, dtype=np.float64) / config.sample_rate

    output.parent.mkdir(parents=True, exist_ok=True)
    total_sounds = (
        len(config.frequencies) * config.phase_count
        + len(config.residual_bands)
        * len(RESIDUAL_KINDS)
        * config.residual_variant_count
    )
    completed_sounds = 0
    with temporary_directory(
        ".wav2mc-bank-",
        directory=output.parent,
    ) as staging:
        root = staging / "content"
        staged_output = staging / output.name
        emit_progress(progress_callback, "resource_pack", 0.0, "Building sounds")
        check_cancelled(cancel_check)
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
                check_cancelled(cancel_check)
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
                completed_sounds += 1
                if completed_sounds % 16 == 0:
                    emit_progress(
                        progress_callback,
                        "resource_pack",
                        0.84 * completed_sounds / max(1, total_sounds),
                        f"Generated {completed_sounds}/{total_sounds} sounds",
                    )

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
                    check_cancelled(cancel_check)
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
                    completed_sounds += 1
                    emit_progress(
                        progress_callback,
                        "resource_pack",
                        0.84 * completed_sounds / max(1, total_sounds),
                        f"Generated {completed_sounds}/{total_sounds} sounds",
                    )

        write_json(root / "assets" / namespace / "sounds.json", sounds)
        zip_directory(
            root,
            staged_output,
            progress_callback=scaled_progress(
                progress_callback,
                0.84,
                0.99,
                "resource_pack",
            ),
            cancel_check=cancel_check,
        )
        check_cancelled(cancel_check)
        staged_output.replace(output)
        emit_progress(
            progress_callback,
            "resource_pack",
            1.0,
            "Resource pack complete",
        )


def build_device_pack_set(
    output_dir: Path,
    base_config: AudioConfig,
    pack_format: float,
    namespace_prefix: str = "wav2mc",
    grain_level: float = 1.0,
    profile_names: tuple[str, ...] = DEFAULT_DEVICE_PACK_PROFILES,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, Path]:
    if not profile_names:
        raise ValueError("At least one device profile is required")

    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Path] = {}
    manifest_profiles: dict[str, object] = {}
    with temporary_directory(
        ".wav2mc-bank-set-",
        directory=output_dir,
    ) as staging:
        for index, profile_name in enumerate(profile_names):
            check_cancelled(cancel_check)
            try:
                profile = DEVICE_PROFILES[profile_name]
            except KeyError as exc:
                raise ValueError(f"Unknown device profile: {profile_name}") from exc

            config = device_audio_config(base_config, profile)
            namespace = safe_namespace(f"{namespace_prefix}_{profile.name}")
            target = output_dir / f"{namespace}_sine_bank.zip"
            staged_target = staging / target.name
            build_resource_pack(
                output=staged_target,
                config=config,
                pack_format=pack_format,
                namespace=namespace,
                grain_level=grain_level,
                device_profile=profile.name,
                progress_callback=scaled_progress(
                    progress_callback,
                    index / len(profile_names),
                    (index + 1) / len(profile_names),
                    "resource_pack",
                ),
                cancel_check=cancel_check,
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

        staged_manifest = staging / "wav2mc-device-packs.json"
        write_json(
            staged_manifest,
            {
                "minecraft_version": DEFAULT_MINECRAFT_VERSION,
                "pack_format": pack_format,
                "grain_level": grain_level,
                "profiles": manifest_profiles,
            },
        )
        check_cancelled(cancel_check)

        staged_paths = {
            **{
                profile_name: staging / path.name
                for profile_name, path in outputs.items()
            },
            "manifest": staged_manifest,
        }
        backup_dir = staging / "backups"
        backup_dir.mkdir()
        committed: list[Path] = []
        backups: dict[Path, Path] = {}
        try:
            for staged_path in staged_paths.values():
                final_path = output_dir / staged_path.name
                if final_path.exists():
                    backup = backup_dir / final_path.name
                    final_path.replace(backup)
                    backups[final_path] = backup
                staged_path.replace(final_path)
                committed.append(final_path)
        except OSError:
            for final_path in committed:
                final_path.unlink(missing_ok=True)
            for final_path, backup in backups.items():
                backup.replace(final_path)
            raise

    outputs["manifest"] = output_dir / "wav2mc-device-packs.json"
    emit_progress(progress_callback, "resource_pack", 1.0, "Pack set complete")
    return outputs
