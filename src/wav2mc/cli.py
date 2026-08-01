from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from .bank import build_device_pack_set, build_resource_pack
from .config import (
    DEVICE_PROFILES,
    DEFAULT_DEVICE_PACK_PROFILES,
    DEFAULT_DATA_PACK_FORMAT,
    DEFAULT_LAYOUT,
    DEFAULT_RESOURCE_PACK_FORMAT,
    QUALITY_PROFILES,
    AudioConfig,
    LoudnessCalibration,
    device_audio_config,
)
from .pipeline import convert_audio


def _add_audio_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sample-rate", type=int, default=48_000)
    parser.add_argument("--grain-ms", type=float, default=100.0)
    parser.add_argument("--hop-ms", type=float, default=50.0)
    parser.add_argument("--min-frequency", type=int, default=20)
    parser.add_argument("--max-frequency", type=int, default=20000)
    parser.add_argument(
        "--frequency-grid",
        choices=("adaptive", "uniform"),
        default=None,
        help="Adaptive by default; explicit --frequency-step selects uniform",
    )
    parser.add_argument("--frequency-step", type=int, default=None)
    parser.add_argument("--phases", type=int, default=16)
    parser.add_argument(
        "--hybrid-residual",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Preserve generic transients and stochastic residual bands",
    )
    parser.add_argument("--residual-variants", type=int, default=4)


def _audio_config(args: argparse.Namespace) -> AudioConfig:
    adaptive_frequency_grid = args.frequency_grid == "adaptive" or (
        args.frequency_grid is None and args.frequency_step is None
    )
    config = AudioConfig(
        sample_rate=args.sample_rate,
        grain_ms=args.grain_ms,
        hop_ms=args.hop_ms,
        min_frequency=args.min_frequency,
        max_frequency=args.max_frequency,
        frequency_step=args.frequency_step or 20,
        phase_count=args.phases,
        adaptive_frequency_grid=adaptive_frequency_grid,
        hybrid_residual=args.hybrid_residual,
        residual_variant_count=args.residual_variants,
    )
    if config.hop_size * 2 != config.window_size:
        raise ValueError("The base implementation requires grain-ms = 2 * hop-ms")
    if (
        config.frequency_step <= 0
        or config.phase_count <= 0
        or config.residual_variant_count <= 0
    ):
        raise ValueError(
            "frequency-step, phases, and residual-variants must be positive"
        )
    if config.min_frequency <= 0 or config.max_frequency < config.min_frequency:
        raise ValueError("Invalid frequency range")
    profile_name = getattr(args, "device_profile", None)
    if profile_name:
        config = device_audio_config(config, DEVICE_PROFILES[profile_name])
    return config


def _profile_namespace(value: str | None, profile_name: str | None) -> str:
    if value:
        return value
    return f"wav2mc_{profile_name}" if profile_name else "wav2mc"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wav2mc",
        description=(
            "Generate a reusable sine-grain resource pack and convert audio "
            "into a Minecraft Java data pack."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    gui_parser = subparsers.add_parser(
        "gui",
        help="Launch the desktop graphical interface",
    )
    gui_parser.add_argument(
        "input",
        nargs="?",
        type=Path,
        default=None,
        help="Optional audio or media file to preselect",
    )
    gui_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output"),
    )

    bank_parser = subparsers.add_parser(
        "bank-build",
        help="Build the reusable sine-grain resource pack",
    )
    bank_parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/wav2mc_sine_bank.zip"),
    )
    bank_parser.add_argument(
        "--pack-format",
        type=float,
        default=DEFAULT_RESOURCE_PACK_FORMAT,
    )
    bank_parser.add_argument("--namespace", default=None)
    bank_parser.add_argument("--grain-level", type=float, default=1.0)
    bank_parser.add_argument(
        "--device-profile",
        "--mode",
        dest="device_profile",
        choices=sorted(DEVICE_PROFILES),
        default=None,
    )
    _add_audio_config(bank_parser)

    set_parser = subparsers.add_parser(
        "bank-build-set",
        help="Build recommended adaptive-grid resource packs",
    )
    set_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/device_banks"),
    )
    set_parser.add_argument(
        "--profiles",
        nargs="+",
        choices=sorted(DEVICE_PROFILES),
        default=list(DEFAULT_DEVICE_PACK_PROFILES),
    )
    set_parser.add_argument(
        "--pack-format",
        type=float,
        default=DEFAULT_RESOURCE_PACK_FORMAT,
    )
    set_parser.add_argument("--namespace-prefix", default="wav2mc")
    set_parser.add_argument("--grain-level", type=float, default=1.0)
    _add_audio_config(set_parser)

    convert_parser = subparsers.add_parser(
        "convert",
        help="Convert any FFmpeg-supported audio file",
    )
    convert_parser.add_argument(
        "input",
        type=Path,
        help="Local audio or media file decoded by FFmpeg",
    )
    convert_parser.add_argument(
        "--audio-stream",
        type=int,
        default=0,
        help="Zero-based audio stream index for media containers",
    )
    convert_parser.add_argument("--name", default=None)
    convert_parser.add_argument("--output-dir", type=Path, default=Path("output"))
    convert_parser.add_argument(
        "--quality",
        choices=sorted(QUALITY_PROFILES),
        default=None,
    )
    convert_parser.add_argument(
        "--data-pack-format",
        type=float,
        default=DEFAULT_DATA_PACK_FORMAT,
    )
    convert_parser.add_argument(
        "--layout",
        choices=("modern", "legacy"),
        default=DEFAULT_LAYOUT,
        help="modern: data/<ns>/function; legacy: data/<ns>/functions",
    )
    convert_parser.add_argument("--bank-namespace", default=None)
    convert_parser.add_argument("--category", default="record")
    convert_parser.add_argument("--gain", type=float, default=1.0)
    convert_parser.add_argument(
        "--stereo",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Preserve stereo input; --no-stereo downmixes to mono",
    )
    convert_parser.add_argument("--bank-grain-level", type=float, default=1.0)
    convert_parser.add_argument(
        "--device-profile",
        "--mode",
        dest="device_profile",
        choices=sorted(DEVICE_PROFILES),
        default=None,
    )
    convert_parser.add_argument(
        "--psychoacoustic-masking",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable A-weighted Bark-domain masking",
    )
    convert_parser.add_argument(
        "--masking-offset-db",
        type=float,
        default=None,
        help="Higher values retain more peaks (quality profile default if omitted)",
    )
    convert_parser.add_argument(
        "--minecraft-gain",
        type=float,
        default=1.0,
        help="Measured Minecraft/reference amplitude ratio at volume 1.0",
    )
    convert_parser.add_argument(
        "--minecraft-volume-exponent",
        type=float,
        default=1.0,
        help="Exponent of the measured command-volume response curve",
    )
    convert_parser.add_argument(
        "--max-command-volume",
        type=float,
        default=1.0,
        help="Maximum /playsound volume emitted by the data pack",
    )
    _add_audio_config(convert_parser)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "gui":
            try:
                from .gui import launch_gui
            except ImportError as exc:
                raise RuntimeError(
                    "Tkinter is required for the GUI. Install the Tk package "
                    "for your Python distribution."
                ) from exc
            launch_gui(
                initial_input=args.input,
                output_dir=args.output_dir,
            )
            return 0

        config = _audio_config(args)
        if args.command == "bank-build":
            namespace = _profile_namespace(args.namespace, args.device_profile)
            print(f"Building resource pack: {args.output}")
            build_resource_pack(
                output=args.output,
                config=config,
                pack_format=args.pack_format,
                namespace=namespace,
                grain_level=args.grain_level,
                device_profile=args.device_profile,
            )
            print(f"Created: {args.output.resolve()}")
            return 0

        if args.command == "bank-build-set":
            print(f"Building device pack set: {args.output_dir}")
            outputs = build_device_pack_set(
                output_dir=args.output_dir,
                base_config=config,
                pack_format=args.pack_format,
                namespace_prefix=args.namespace_prefix,
                grain_level=args.grain_level,
                profile_names=tuple(args.profiles),
            )
            for label, path in outputs.items():
                print(f"{label}: {path.resolve()}")
            return 0

        if args.command == "convert":
            profile = (
                DEVICE_PROFILES[args.device_profile]
                if args.device_profile
                else None
            )
            quality_name = args.quality or (
                profile.quality_name if profile else "normal"
            )
            quality = QUALITY_PROFILES[quality_name]
            if args.masking_offset_db is not None:
                quality = replace(
                    quality,
                    masking_offset_db=args.masking_offset_db,
                )
            calibration = LoudnessCalibration(
                minecraft_gain=args.minecraft_gain,
                volume_exponent=args.minecraft_volume_exponent,
                max_command_volume=args.max_command_volume,
            )
            song_name = args.name or args.input.stem
            print(f"Converting: {args.input}")
            outputs = convert_audio(
                source=args.input,
                output_dir=args.output_dir,
                song_name=song_name,
                config=config,
                quality=quality,
                data_pack_format=args.data_pack_format,
                layout=args.layout,
                bank_namespace=_profile_namespace(
                    args.bank_namespace,
                    args.device_profile,
                ),
                category=args.category,
                requested_gain=args.gain,
                bank_grain_level=args.bank_grain_level,
                loudness_calibration=calibration,
                psychoacoustic_masking=args.psychoacoustic_masking,
                device_profile=args.device_profile,
                audio_stream=args.audio_stream,
                preserve_stereo=args.stereo,
            )
            for label, path in outputs.items():
                print(f"{label}: {path.resolve()}")
            return 0

        parser.error("Unknown command")
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
