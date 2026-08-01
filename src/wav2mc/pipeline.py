from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import numpy as np

from .analysis import analyse_audio, scale_frames
from .audio import load_mono, peak_normalize, preprocess_audio, write_wav
from .config import AudioConfig, QualityProfile
from .datapack import build_data_pack
from .preview import calculate_safe_scale, synthesize_preview
from .utils import safe_namespace, temporary_directory, write_json


def convert_audio(
    source: Path,
    output_dir: Path,
    song_name: str,
    config: AudioConfig,
    quality: QualityProfile,
    data_pack_format: int,
    layout: str,
    bank_namespace: str,
    category: str,
    requested_gain: float,
    bank_grain_level: float,
) -> dict[str, Path]:
    if not source.is_file():
        raise FileNotFoundError(source)
    if requested_gain <= 0:
        raise ValueError("requested_gain must be positive")
    if not 0.0 < bank_grain_level <= 1.0:
        raise ValueError("bank_grain_level must be in (0, 1]")

    output_dir.mkdir(parents=True, exist_ok=True)
    namespace = safe_namespace(song_name)
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
        )
        audio = load_mono(decoded, config.sample_rate)
        audio = peak_normalize(audio, target_peak=0.92)

    frames = analyse_audio(audio, config, quality)
    rough_preview = synthesize_preview(frames, config)
    safe_scale = calculate_safe_scale(
        rough_preview,
        target_peak=0.88,
        requested_gain=requested_gain,
    )
    maximum_component = max(
        (component.amplitude for frame in frames for component in frame.components),
        default=0.0,
    )
    if maximum_component > 0.0:
        safe_scale = min(safe_scale, bank_grain_level / maximum_component)

    target_frames = scale_frames(frames, safe_scale)
    preview = synthesize_preview(target_frames, config)
    write_wav(preview_path, preview, config.sample_rate)

    command_frames = scale_frames(target_frames, 1.0 / bank_grain_level)
    build_data_pack(
        data_pack_path,
        frames=command_frames,
        namespace=namespace,
        bank_namespace=bank_namespace,
        pack_format=data_pack_format,
        layout=layout,
        category=category,
    )

    component_counts = np.asarray([len(frame.components) for frame in command_frames], dtype=int)
    report = {
        "source": str(source.resolve()),
        "song_namespace": namespace,
        "input_duration_seconds": round(audio.size / config.sample_rate, 6),
        "output_duration_seconds": round(preview.size / config.sample_rate, 6),
        "frame_count": len(frames),
        "tick_rate": 20,
        "quality": quality.name,
        "safe_scale": safe_scale,
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
        "audio_config": asdict(config),
        "required_resource_pack": {
            "namespace": bank_namespace,
            "sample_rate": config.sample_rate,
            "grain_ms": config.grain_ms,
            "hop_ms": config.hop_ms,
            "min_frequency": config.min_frequency,
            "max_frequency": config.max_frequency,
            "frequency_step": config.frequency_step,
            "phase_count": config.phase_count,
            "grain_level": bank_grain_level,
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
