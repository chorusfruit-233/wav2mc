from __future__ import annotations

from pathlib import Path

from .analysis import AudioFrame
from .bank import sound_event_name
from .config import DEFAULT_MINECRAFT_VERSION, LoudnessCalibration
from .grains import residual_event_name
from .loudness import minecraft_command_volume
from .utils import pack_metadata, temporary_directory, write_json, zip_directory


def _layout_directories(layout: str) -> tuple[str, str]:
    if layout == "modern":
        return "function", "tags/function"
    if layout == "legacy":
        return "functions", "tags/functions"
    raise ValueError("layout must be 'modern' or 'legacy'")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def _range_spec(low: int, high: int) -> str:
    return str(low) if low == high else f"{low}..{high}"


def _create_dispatch_tree(
    function_root: Path,
    namespace: str,
    frame_count: int,
    leaf_span: int = 16,
) -> None:
    dispatch_root = function_root / "dispatch"

    def build(low: int, high: int, name: str) -> None:
        path = dispatch_root / f"{name}.mcfunction"
        if high - low + 1 <= leaf_span:
            lines = [
                (
                    f"execute if score #frame wav2mc matches {index} "
                    f"run function {namespace}:frame/{index:06d}"
                )
                for index in range(low, high + 1)
            ]
            _write_text(path, "\n".join(lines))
            return

        midpoint = (low + high) // 2
        left_name = f"n_{low:06d}_{midpoint:06d}"
        right_name = f"n_{midpoint + 1:06d}_{high:06d}"
        lines = [
            (
                f"execute if score #frame wav2mc matches {_range_spec(low, midpoint)} "
                f"run function {namespace}:dispatch/{left_name}"
            ),
            (
                f"execute if score #frame wav2mc matches {_range_spec(midpoint + 1, high)} "
                f"run function {namespace}:dispatch/{right_name}"
            ),
        ]
        _write_text(path, "\n".join(lines))
        build(low, midpoint, left_name)
        build(midpoint + 1, high, right_name)

    if frame_count <= 0:
        _write_text(dispatch_root / "root.mcfunction", "# no frames")
    else:
        build(0, frame_count - 1, "root")


def build_data_pack(
    output: Path,
    frames: list[AudioFrame],
    namespace: str,
    bank_namespace: str,
    pack_format: float,
    layout: str,
    category: str = "record",
    bank_grain_level: float = 1.0,
    loudness_calibration: LoudnessCalibration | None = None,
) -> None:
    calibration = loudness_calibration or LoudnessCalibration()
    calibration.validate()
    function_dir, tag_dir = _layout_directories(layout)
    frame_count = len(frames)

    with temporary_directory("wav2mc-datapack-") as root:
        write_json(
            root / "pack.mcmeta",
            {
                "pack": pack_metadata(
                    pack_format,
                    f"wav2mc song: {namespace}",
                )
            },
        )
        write_json(
            root / "wav2mc-song.json",
            {
                "minecraft_version": DEFAULT_MINECRAFT_VERSION,
                "namespace": namespace,
                "bank_namespace": bank_namespace,
                "frames": frame_count,
                "tick_rate": 20,
                "category": category,
                "bank_grain_level": bank_grain_level,
                "loudness_calibration": {
                    "minecraft_gain": calibration.minecraft_gain,
                    "volume_exponent": calibration.volume_exponent,
                    "max_command_volume": calibration.max_command_volume,
                },
            },
        )

        function_root = root / "data" / namespace / function_dir
        frame_root = function_root / "frame"
        frame_root.mkdir(parents=True, exist_ok=True)

        for frame in frames:
            lines = []
            for component in frame.components:
                event = sound_event_name(component.frequency, component.phase_index)
                volume = minecraft_command_volume(
                    component.amplitude,
                    bank_grain_level,
                    calibration,
                )
                lines.append(
                    "execute as @a[tag=wav2mc_listener] at @s run playsound "
                    f"{bank_namespace}:{event} {category} @s ~ ~ ~ "
                    f"{volume:.6f} 1.0 0.0"
                )
            for component in frame.residual_components:
                event = residual_event_name(
                    component.kind,
                    component.band_index,
                    component.variant,
                )
                volume = minecraft_command_volume(
                    component.amplitude,
                    bank_grain_level,
                    calibration,
                )
                lines.append(
                    "execute as @a[tag=wav2mc_listener] at @s run playsound "
                    f"{bank_namespace}:{event} {category} @s ~ ~ ~ "
                    f"{volume:.6f} 1.0 0.0"
                )
            if not lines:
                lines = ["# silent frame"]
            _write_text(frame_root / f"{frame.index:06d}.mcfunction", "\n".join(lines))

        _create_dispatch_tree(function_root, namespace, frame_count)

        _write_text(
            function_root / "load.mcfunction",
            "\n".join(
                [
                    "scoreboard objectives add wav2mc dummy",
                    "scoreboard players set #playing wav2mc 0",
                    "scoreboard players set #frame wav2mc 0",
                ]
            ),
        )
        _write_text(
            function_root / "start.mcfunction",
            "\n".join(
                [
                    "tag @a remove wav2mc_listener",
                    "tag @s add wav2mc_listener",
                    "scoreboard players set #frame wav2mc 0",
                    "scoreboard players set #playing wav2mc 1",
                    f'tellraw @s {{"text":"Playing {namespace}","color":"green"}}',
                ]
            ),
        )
        _write_text(
            function_root / "stop.mcfunction",
            "\n".join(
                [
                    "scoreboard players set #playing wav2mc 0",
                    f"stopsound @a[tag=wav2mc_listener] {category}",
                    "tag @a remove wav2mc_listener",
                    "scoreboard players set #frame wav2mc 0",
                ]
            ),
        )
        _write_text(
            function_root / "finish.mcfunction",
            "\n".join(
                [
                    "scoreboard players set #playing wav2mc 0",
                    "scoreboard players set #frame wav2mc 0",
                    "tag @a remove wav2mc_listener",
                ]
            ),
        )
        _write_text(
            function_root / "tick.mcfunction",
            "\n".join(
                [
                    (
                        f"execute if score #playing wav2mc matches 1 "
                        f"run function {namespace}:dispatch/root"
                    ),
                    (
                        "execute if score #playing wav2mc matches 1 "
                        "run scoreboard players add #frame wav2mc 1"
                    ),
                    (
                        f"execute if score #frame wav2mc matches {frame_count}.. "
                        f"run function {namespace}:finish"
                    ),
                ]
            ),
        )

        minecraft_tags = root / "data" / "minecraft" / tag_dir
        write_json(minecraft_tags / "load.json", {"values": [f"{namespace}:load"]})
        write_json(minecraft_tags / "tick.json", {"values": [f"{namespace}:tick"]})

        _write_text(
            root / "README.txt",
            (
                f"Run /function {namespace}:start as a player to begin.\n"
                f"Run /function {namespace}:stop to stop.\n"
                "Install and enable the matching wav2mc hybrid resource pack first."
            ),
        )
        zip_directory(root, output)
