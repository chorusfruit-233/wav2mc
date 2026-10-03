from __future__ import annotations

from pathlib import Path

import numpy as np

from .config import (
    DEVICE_PROFILES,
    DEFAULT_DEVICE_PACK_PROFILES,
    DEFAULT_MINECRAFT_VERSION,
    AudioConfig,
    audio_config_metadata,
    device_audio_config,
)
from .grains import (
    RESIDUAL_KINDS, bank_sound_counts, encode_ogg, residual_event_name,
    residual_grain, residual_variants, tonal_event_name, tonal_grain, tonal_variants,
)
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


def sound_event_name(
    frequency: int, phase_index: int, chirp_rate: int = 0, envelope: str = "hann",
) -> str:
    return tonal_event_name(frequency, phase_index, chirp_rate, envelope)


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
    output.parent.mkdir(parents=True, exist_ok=True)
    total_sounds = sum(bank_sound_counts(config))
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
        def write_grain(event: str, audio: np.ndarray) -> None:
            nonlocal completed_sounds
            check_cancelled(cancel_check)
            relative = event.replace(".", "/")
            path = root / "assets" / namespace / "sounds" / f"{relative}.ogg"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(encode_ogg(grain_level * audio, config.sample_rate))
            sounds[event] = {"sounds": [{"name": f"{namespace}:{relative}", "stream": False}]}
            completed_sounds += 1
            if completed_sounds % 16 == 0 or completed_sounds == total_sounds:
                emit_progress(
                    progress_callback, "resource_pack",
                    0.84 * completed_sounds / max(1, total_sounds),
                    f"Generated {completed_sounds}/{total_sounds} sounds",
                )

        for frequency in config.frequencies:
            for rate, envelope in tonal_variants(frequency, config.sample_rate, n):
                for phase_index in range(config.phase_count):
                    write_grain(
                        sound_event_name(frequency, phase_index, rate, envelope),
                        tonal_grain(config.sample_rate, n, frequency, phase_index,
                                    config.phase_count, rate, envelope),
                    )

        for band_index, low, high in config.residual_bands:
            for kind in RESIDUAL_KINDS:
                for variant in range(config.residual_variant_count):
                    for delay, shape, polarity in residual_variants(kind):
                        write_grain(
                            residual_event_name(kind, band_index, variant, delay, shape, polarity),
                            residual_grain(config.sample_rate, n, band_index, low, high,
                                           variant, kind, delay, shape, polarity),
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
                "sound_count": sum(bank_sound_counts(config)),
                "residual_sound_count": bank_sound_counts(config)[1],
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
