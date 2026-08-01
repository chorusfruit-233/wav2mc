from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np

from .analysis import AudioFrame, analyse_audio, scale_frame_residuals
from .audio import load_mono, peak_normalize, preprocess_audio, write_wav
from .config import (
    DEFAULT_MINECRAFT_VERSION,
    AudioConfig,
    LoudnessCalibration,
    QualityProfile,
    audio_config_metadata,
)
from .datapack import build_data_pack
from .loudness import (
    maximum_reproducible_amplitude,
    minecraft_command_volume,
    predicted_minecraft_amplitude,
)
from .preview import (
    calculate_safe_scale,
    synthesize_preview,
    synthesize_residual_frame,
)
from .utils import safe_namespace, temporary_directory, write_json


RESIDUAL_GAIN_LIMITS = {
    "noise": 0.70,
    "transient": 0.40,
}
RESIDUAL_LAYER_ORDER = ("transient", "noise")


def _maximum_additive_scale(
    base: np.ndarray,
    addition: np.ndarray,
    maximum_scale: float,
    target_peak: float,
) -> float:
    limit = maximum_scale
    positive = addition > 1e-12
    if np.any(positive):
        limit = min(
            limit,
            float(np.min((target_peak - base[positive]) / addition[positive])),
        )
    negative = addition < -1e-12
    if np.any(negative):
        limit = min(
            limit,
            float(np.min((-target_peak - base[negative]) / addition[negative])),
        )
    return float(np.clip(limit, 0.0, maximum_scale))


def _residual_frame_scales(
    frames: list[AudioFrame],
    config: AudioConfig,
    tone_preview: np.ndarray,
    tone_scale: float,
    requested_gain: float,
    maximum_supported: float,
    target_peak: float = 0.88,
    release_alpha: float = 0.25,
    gain_limits: dict[str, float] | None = None,
) -> dict[str, list[float]]:
    limits = dict(gain_limits or RESIDUAL_GAIN_LIMITS)
    if set(limits) != set(RESIDUAL_LAYER_ORDER):
        raise ValueError("Residual gain limits must define noise and transient")
    if any(not 0.0 <= limit <= 1.0 for limit in limits.values()):
        raise ValueError("Residual gain limits must be between 0 and 1")

    residual_mix = np.zeros_like(tone_preview)
    scales = {kind: [] for kind in RESIDUAL_LAYER_ORDER}
    previous_scales = {
        kind: requested_gain * limits[kind]
        for kind in RESIDUAL_LAYER_ORDER
    }

    for frame in frames:
        start = frame.index * config.hop_size
        end = start + config.window_size
        for kind in RESIDUAL_LAYER_ORDER:
            maximum_component = max(
                (
                    component.amplitude
                    for component in frame.residual_components
                    if component.kind == kind
                ),
                default=0.0,
            )
            scale_cap = requested_gain * limits[kind]
            if maximum_component > 0.0:
                scale_cap = min(
                    scale_cap,
                    maximum_supported / maximum_component,
                )

            addition = synthesize_residual_frame(frame, config, kind=kind)
            base = tone_scale * tone_preview[start:end] + residual_mix[start:end]
            raw_limit = _maximum_additive_scale(
                base,
                addition,
                scale_cap,
                target_peak,
            )
            recovery_limit = scale_cap
            previous_scale = previous_scales[kind]
            if scale_cap > previous_scale:
                recovery_limit = previous_scale + release_alpha * (
                    scale_cap - previous_scale
                )
            frame_scale = min(raw_limit, recovery_limit)
            residual_mix[start:end] += frame_scale * addition
            scales[kind].append(frame_scale)
            previous_scales[kind] = frame_scale

    return scales


def _scale_statistics(
    frames: list[AudioFrame],
    scales: list[float],
    kind: str,
) -> dict[str, float]:
    active = np.asarray(
        [
            scale
            for frame, scale in zip(frames, scales, strict=True)
            if any(component.kind == kind for component in frame.residual_components)
        ],
        dtype=np.float64,
    )
    if not active.size:
        active = np.asarray(scales, dtype=np.float64)
    return {
        "mean": float(active.mean()),
        "minimum": float(active.min()),
        "maximum": float(active.max()),
        "p10": float(np.percentile(active, 10)),
        "p90": float(np.percentile(active, 90)),
    }


def convert_audio(
    source: Path,
    output_dir: Path,
    song_name: str,
    config: AudioConfig,
    quality: QualityProfile,
    data_pack_format: float,
    layout: str,
    bank_namespace: str,
    category: str,
    requested_gain: float,
    bank_grain_level: float,
    loudness_calibration: LoudnessCalibration | None = None,
    psychoacoustic_masking: bool = True,
    device_profile: str | None = None,
    audio_stream: int = 0,
) -> dict[str, Path]:
    calibration = loudness_calibration or LoudnessCalibration()
    calibration.validate()
    if not source.is_file():
        raise FileNotFoundError(source)
    if requested_gain <= 0:
        raise ValueError("requested_gain must be positive")
    if not 0.0 < bank_grain_level <= 1.0:
        raise ValueError("bank_grain_level must be in (0, 1]")

    output_dir.mkdir(parents=True, exist_ok=True)
    namespace = safe_namespace(song_name)
    resolved_source = source.resolve()
    try:
        report_source = str(resolved_source.relative_to(Path.cwd().resolve()))
    except ValueError:
        report_source = str(resolved_source)
    data_pack_path = output_dir / f"{namespace}_datapack.zip"
    preview_path = output_dir / f"{namespace}_preview.wav"
    report_path = output_dir / f"{namespace}_analysis.json"

    with temporary_directory("wav2mc-convert-") as temp:
        decoded = temp / "decoded.wav"
        preprocess_audio(
            source,
            decoded,
            sample_rate=config.sample_rate,
            low_frequency=max(20, config.min_frequency // 2),
            high_frequency=min(config.max_frequency, config.sample_rate // 2 - 100),
            audio_stream=audio_stream,
        )
        audio = load_mono(decoded, config.sample_rate)
        audio = peak_normalize(audio, target_peak=0.92)

    frames = analyse_audio(
        audio,
        config,
        quality,
        psychoacoustic_masking=psychoacoustic_masking,
    )
    tone_frames = [replace(frame, residual_components=()) for frame in frames]
    tone_preview = synthesize_preview(tone_frames, config)
    tone_scale = calculate_safe_scale(
        tone_preview,
        target_peak=0.88,
        requested_gain=requested_gain,
    )
    maximum_tone_component = max(
        (
            component.amplitude
            for frame in frames
            for component in frame.components
        ),
        default=0.0,
    )
    maximum_supported = maximum_reproducible_amplitude(
        bank_grain_level,
        calibration,
    )
    if maximum_tone_component > 0.0:
        tone_scale = min(
            tone_scale,
            maximum_supported / maximum_tone_component,
        )

    residual_scales = _residual_frame_scales(
        frames,
        config,
        tone_preview,
        tone_scale,
        requested_gain,
        maximum_supported,
    )
    target_frames = scale_frame_residuals(
        frames,
        tone_scale,
        residual_scales,
    )
    preview = synthesize_preview(target_frames, config)
    write_wav(preview_path, preview, config.sample_rate)

    build_data_pack(
        data_pack_path,
        frames=target_frames,
        namespace=namespace,
        bank_namespace=bank_namespace,
        pack_format=data_pack_format,
        layout=layout,
        category=category,
        bank_grain_level=bank_grain_level,
        loudness_calibration=calibration,
    )

    tone_counts = np.asarray(
        [len(frame.components) for frame in target_frames],
        dtype=int,
    )
    noise_counts = np.asarray(
        [
            sum(
                component.kind == "noise"
                for component in frame.residual_components
            )
            for frame in target_frames
        ],
        dtype=int,
    )
    transient_counts = np.asarray(
        [
            sum(
                component.kind == "transient"
                for component in frame.residual_components
            )
            for frame in target_frames
        ],
        dtype=int,
    )
    component_counts = tone_counts + noise_counts + transient_counts
    residual_scale_statistics = {
        kind: _scale_statistics(frames, residual_scales[kind], kind)
        for kind in RESIDUAL_LAYER_ORDER
    }
    amplitude_predictions = []
    command_volumes = []
    for frame in target_frames:
        for component in frame.all_components:
            command_volume = minecraft_command_volume(
                component.amplitude,
                bank_grain_level,
                calibration,
            )
            rounded_volume = round(command_volume, 6)
            command_volumes.append(rounded_volume)
            amplitude_predictions.append(
                abs(
                    predicted_minecraft_amplitude(
                        rounded_volume,
                        bank_grain_level,
                        calibration,
                    )
                    - component.amplitude
                )
            )
    report = {
        "minecraft_version": DEFAULT_MINECRAFT_VERSION,
        "source": report_source,
        "input_audio_stream": audio_stream,
        "song_namespace": namespace,
        "input_duration_seconds": round(audio.size / config.sample_rate, 6),
        "output_duration_seconds": round(preview.size / config.sample_rate, 6),
        "frame_count": len(frames),
        "tick_rate": 20,
        "quality": quality.name,
        "quality_profile": {
            "max_components": quality.max_components,
            "max_noise_components": quality.max_noise_components,
            "max_transient_components": quality.max_transient_components,
            "residual_floor_db": quality.residual_floor_db,
            "band_budgets": [
                {
                    "min_frequency": low,
                    "max_frequency": high,
                    "max_components": count,
                }
                for low, high, count in quality.band_limits
            ],
        },
        "device_profile": device_profile,
        "psychoacoustic_masking": {
            "enabled": psychoacoustic_masking,
            "model": "A-weighted asymmetric Bark spreading",
            "masking_offset_db": quality.masking_offset_db,
        },
        "safe_scale": tone_scale,
        "layer_scales": {
            "tone": tone_scale,
            "residual": {
                kind: {
                    **residual_scale_statistics[kind],
                    "gain_limit": RESIDUAL_GAIN_LIMITS[kind],
                }
                for kind in RESIDUAL_LAYER_ORDER
            } | {
                "release_ms": 200,
            },
        },
        "preview_peak": float(np.max(np.abs(preview))) if preview.size else 0.0,
        "average_components_per_frame": (
            float(component_counts.mean()) if component_counts.size else 0.0
        ),
        "maximum_components_per_frame": (
            int(component_counts.max()) if component_counts.size else 0
        ),
        "estimated_playsound_commands_per_second": (
            float(component_counts.mean() * 20) if component_counts.size else 0.0
        ),
        "component_model": {
            "name": "hybrid-tonal-transient-noise",
            "hybrid_residual_enabled": config.hybrid_residual,
            "noise_amplitude_model": "band RMS * spectral flatness ^ 0.25",
            "noise_variant_model": "tracked deterministic sequence",
            "transient_hysteresis": {
                "cooldown_ms": 50,
                "forced_rearm_ms": 100,
                "minimum_band_growth_db": 2.0,
                "minimum_new_energy_ratio": 0.35,
            },
            "average_tone_components_per_frame": (
                float(tone_counts.mean()) if tone_counts.size else 0.0
            ),
            "average_noise_components_per_frame": (
                float(noise_counts.mean()) if noise_counts.size else 0.0
            ),
            "average_transient_components_per_frame": (
                float(transient_counts.mean()) if transient_counts.size else 0.0
            ),
            "maximum_noise_components_per_frame": (
                int(noise_counts.max()) if noise_counts.size else 0
            ),
            "maximum_transient_components_per_frame": (
                int(transient_counts.max()) if transient_counts.size else 0
            ),
        },
        "loudness_calibration": {
            "minecraft_gain": calibration.minecraft_gain,
            "volume_exponent": calibration.volume_exponent,
            "max_command_volume": calibration.max_command_volume,
            "maximum_reproducible_component_amplitude": (
                maximum_reproducible_amplitude(bank_grain_level, calibration)
            ),
            "maximum_command_volume_used": max(command_volumes, default=0.0),
            "maximum_predicted_amplitude_error": max(
                amplitude_predictions,
                default=0.0,
            ),
        },
        "audio_config": audio_config_metadata(config),
        "required_resource_pack": {
            "namespace": bank_namespace,
            **audio_config_metadata(config),
            "grain_level": bank_grain_level,
            "device_profile": device_profile,
        },
        "data_pack": {
            "pack_format": data_pack_format,
            "layout": layout,
            "category": category,
        },
        "outputs": {
            "data_pack": data_pack_path.name,
            "preview": preview_path.name,
            "report": report_path.name,
        },
    }
    write_json(report_path, report)

    return {
        "data_pack": data_pack_path,
        "preview": preview_path,
        "report": report_path,
    }
