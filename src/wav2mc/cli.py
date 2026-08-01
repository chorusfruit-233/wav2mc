from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .bank import build_resource_pack
from .config import (
    DEFAULT_DATA_PACK_FORMAT,
    DEFAULT_LAYOUT,
    DEFAULT_RESOURCE_PACK_FORMAT,
    QUALITY_PROFILES,
    AudioConfig,
)
from .pipeline import convert_audio


def _add_audio_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sample-rate", type=int, default=48_000)
    parser.add_argument("--grain-ms", type=float, default=100.0)
    parser.add_argument("--hop-ms", type=float, default=50.0)
    parser.add_argument("--min-frequency", type=int, default=80)
    parser.add_argument("--max-frequency", type=int, default=8000)
    parser.add_argument("--frequency-step", type=int, default=20)
    parser.add_argument("--phases", type=int, default=16)


def _audio_config(args: argparse.Namespace) -> AudioConfig:
    config = AudioConfig(
        sample_rate=args.sample_rate,
        grain_ms=args.grain_ms,
        hop_ms=args.hop_ms,
        min_frequency=args.min_frequency,
        max_frequency=args.max_frequency,
        frequency_step=args.frequency_step,
        phase_count=args.phases,
    )
    if config.hop_size * 2 != config.window_size:
        raise ValueError("The base implementation requires grain-ms = 2 * hop-ms")
    if config.frequency_step <= 0 or config.phase_count <= 0:
        raise ValueError("frequency-step and phases must be positive")
    if config.min_frequency <= 0 or config.max_frequency < config.min_frequency:
        raise ValueError("Invalid frequency range")
    return config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wav2mc",
        description=(
            "Generate a reusable sine-grain resource pack and convert audio "
            "into a Minecraft Java data pack."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    bank_parser = subparsers.add_parser(
        "bank-build",
        help="Build the reusable sine-grain resource pack",
    )
    bank_parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/wav2mc_sine_bank.zip"),
    )
    bank_parser.add_argument("--pack-format", type=int, default=DEFAULT_RESOURCE_PACK_FORMAT)
    bank_parser.add_argument("--namespace", default="wav2mc")
    bank_parser.add_argument("--grain-level", type=float, default=1.0)
    _add_audio_config(bank_parser)

    convert_parser = subparsers.add_parser(
        "convert",
        help="Convert an audio file into a data pack and preview WAV",
    )
    convert_parser.add_argument("input", type=Path)
    convert_parser.add_argument("--name", default=None)
    convert_parser.add_argument("--output-dir", type=Path, default=Path("output"))
    convert_parser.add_argument(
        "--quality",
        choices=sorted(QUALITY_PROFILES),
        default="normal",
    )
    convert_parser.add_argument(
        "--data-pack-format",
        type=int,
        default=DEFAULT_DATA_PACK_FORMAT,
    )
    convert_parser.add_argument(
        "--layout",
        choices=("modern", "legacy"),
        default=DEFAULT_LAYOUT,
        help="modern: data/<ns>/function; legacy: data/<ns>/functions",
    )
    convert_parser.add_argument("--bank-namespace", default="wav2mc")
    convert_parser.add_argument("--category", default="record")
    convert_parser.add_argument("--gain", type=float, default=1.0)
    convert_parser.add_argument("--bank-grain-level", type=float, default=1.0)
    _add_audio_config(convert_parser)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = _audio_config(args)
        if args.command == "bank-build":
            print(f"Building resource pack: {args.output}")
            build_resource_pack(
                output=args.output,
                config=config,
                pack_format=args.pack_format,
                namespace=args.namespace,
                grain_level=args.grain_level,
            )
            print(f"Created: {args.output.resolve()}")
            return 0

        if args.command == "convert":
            song_name = args.name or args.input.stem
            print(f"Converting: {args.input}")
            outputs = convert_audio(
                source=args.input,
                output_dir=args.output_dir,
                song_name=song_name,
                config=config,
                quality=QUALITY_PROFILES[args.quality],
                data_pack_format=args.data_pack_format,
                layout=args.layout,
                bank_namespace=args.bank_namespace,
                category=args.category,
                requested_gain=args.gain,
                bank_grain_level=args.bank_grain_level,
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
