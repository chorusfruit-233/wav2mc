from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .config import VIDEO_QUALITY_PROFILES, video_quality_name, video_renderer
from .playback import playback_metadata
from .utils import CancelCheck, ProgressCallback, check_cancelled, emit_progress
from .video import VideoFrames
from .video_texture import VIDEO_TILE_SIZE, texture_layout, tile_text_component


VIDEO_DIMENSIONS = (
    "minecraft:overworld", "minecraft:the_nether", "minecraft:the_end",
)


def video_metadata(
    namespace: str, video: VideoFrames, pack_format: float,
) -> dict[str, object]:
    """Describe the vanilla RGB display screen and its two public controls."""
    video.config.validate()
    playback = playback_metadata(namespace, video.tick_count)
    if pack_format < 12:
        raise ValueError("Display-entity video requires Minecraft Java 1.19.4 or newer (data pack format >= 12)")
    if (
        video.frames.dtype != np.uint8
        or video.frames.shape != (
            video.frame_count, video.config.height, video.config.width, 3,
        )
        or video.frame_count <= 0
    ):
        raise ValueError("Video frames must be a nonempty uint8 RGB array matching VideoConfig")
    tag = str(playback["listener_tag"]).replace("wav2mc_", "w2v_", 1)
    config = video.config
    quality = video_quality_name(config)
    texture = video_renderer(config) == "texture"
    tiles = texture_layout(config) if texture else {}
    displays = tiles["display_entities"] if texture else config.width * config.height
    return {
        "renderer": "text_display_texture" if texture else "text_display_background",
        "quality": quality,
        "quality_label": VIDEO_QUALITY_PROFILES[quality].label if quality != "custom" else "自定义",
        "native_resolution": config.native_resolution,
        "width": config.width,
        "height": config.height,
        "fps": config.fps,
        "frame_count": video.frame_count,
        "tick_count": video.tick_count,
        "duration_seconds": video.duration_seconds,
        "pixel_size_blocks": config.pixel_size,
        "pixel_height_blocks": config.pixel_height,
        "pixel_aspect_ratio": config.pixel_aspect_ratio,
        "screen_width_blocks": config.width * config.pixel_size,
        "screen_height_blocks": config.height * config.pixel_height,
        "pixel_entities": 0 if texture else displays,
        "display_entities": displays,
        "entity_count": displays + 1,
        "pixel_updates_per_second": config.width * config.height * config.fps,
        "display_updates_per_second": displays * config.fps,
        "estimated_texture_bytes": displays * VIDEO_TILE_SIZE ** 2 * 4 * video.frame_count if texture else 0,
        "source_width": video.info.width,
        "source_height": video.info.height,
        "source_display_width": video.info.display_width,
        "source_display_height": video.info.display_height,
        **tiles,
        "entity_tag": tag,
        "anchor_tag": f"{tag}_anchor",
        "supported_dimensions": list(VIDEO_DIMENSIONS),
        "requires_resource_pack": texture,
        "text_component_encoding": "snbt" if pack_format >= 71 else "json",
        "commands": {
            "screen_create": f"/function {namespace}:video/screen_create",
            "screen_remove": f"/function {namespace}:video/screen_remove",
        },
    }


def _write_function(root: Path, name: str, lines: list[str]) -> None:
    path = root / f"{name}.mcfunction"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _float(value: float) -> str:
    return f"{value:.9g}f"


def _pixel_nbt(
    x: int, y: int, metadata: dict[str, object], pack_format: float,
) -> str:
    pixel_size = float(metadata["pixel_size_blocks"])
    pixel_height = float(metadata["pixel_height_blocks"])
    width, height = int(metadata["width"]), int(metadata["height"])
    cx = (x - (width - 1) / 2) * pixel_size
    cy = ((height - 1) / 2 - y) * pixel_height
    translation = ",".join(_float(value) for value in (cx - 0.1 * pixel_size, cy - 0.5 * pixel_height, 0))
    scale = ",".join(_float(value) for value in (8 * pixel_size, 4 * pixel_height, 1))
    tag = str(metadata["entity_tag"])
    pixel_tag = f"{tag}_p{y * width + x:04d}"
    component = {"text": " ", "font": "minecraft:default", "bold": False}
    text = json.dumps(component, separators=(",", ":"))
    # Before 1.21.5, display text contains a JSON string rather than an NBT component.
    if pack_format < 71:
        text = json.dumps(text)
    return (
        "{Tags:" + json.dumps([tag, pixel_tag], separators=(",", ":"))
        + ',billboard:"fixed",text:' + text
        + ',text_opacity:-1b,background:-16777216,default_background:0b,'
        'brightness:{block:15,sky:15},shadow:0b,shadow_radius:0f,shadow_strength:0f,'
        'width:0f,height:0f,view_range:2f,line_width:4,interpolation_duration:0,'
        'transformation:{translation:[' + translation + '],scale:[' + scale + '],'
        'left_rotation:[0f,0f,0f,1f],right_rotation:[0f,0f,0f,1f]}}'
    )


def _component_nbt(component: dict[str, object], pack_format: float) -> str:
    text = json.dumps(component, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(text, ensure_ascii=False) if pack_format < 71 else text


def _texture_nbt(
    x: int, y: int, namespace: str, metadata: dict[str, object], pack_format: float,
) -> str:
    """Place an oversampled 256 px glyph at its original pixel coordinates."""
    p = float(metadata["pixel_size_blocks"])
    q = float(metadata["pixel_height_blocks"])
    width, height = int(metadata["width"]), int(metadata["height"])
    columns = int(metadata["tile_columns"])
    tile = y * columns + x
    visible_width = min(VIDEO_TILE_SIZE, width - x * VIDEO_TILE_SIZE)
    # Bitmap fonts round the alpha-scanned width before adding one font unit.
    advance = int(visible_width / 32 + 0.5) + 1
    left = (-width / 2 + x * VIDEO_TILE_SIZE) * p
    top = (height / 2 - y * VIDEO_TILE_SIZE) * q
    translation = ",".join(_float(value) for value in (
        left - (1 - advance / 2) * 32 * p, top - 288 * q, 0,
    ))
    scale = ",".join(_float(value) for value in (1280 * p, 1280 * q, 1))
    tag = str(metadata["entity_tag"])
    text = _component_nbt(tile_text_component(namespace, 0, tile), pack_format)
    return (
        "{Tags:" + json.dumps([tag, f"{tag}_t{tile:04d}"], separators=(",", ":"))
        + ',billboard:"fixed",text:' + text
        + ',text_opacity:-1b,background:0,default_background:0b,alignment:"left",'
        'brightness:{block:15,sky:15},shadow:0b,shadow_radius:0f,shadow_strength:0f,'
        'width:0f,height:0f,view_range:2f,line_width:16,interpolation_duration:0,'
        'transformation:{translation:[' + translation + '],scale:[' + scale + '],'
        'left_rotation:[0f,0f,0f,1f],right_rotation:[0f,0f,0f,1f]}}'
    )


def write_video_functions(
    function_root: Path,
    namespace: str,
    video: VideoFrames,
    pack_format: float,
    functions: dict[str, list[str]],
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, object]:
    """Write full video frames and attach screen controls to the audio timeline.

    All pixels share one entity origin. Geometry lives in the display transform,
    so one screen occupies one chunk even when its visible plane spans chunks.
    Screen generations make unloaded remnants expire when they are loaded again.
    """
    metadata = video_metadata(namespace, video, pack_format)
    objective = str(playback_metadata(namespace, video.tick_count)["objective"])
    tag, anchor_tag = str(metadata["entity_tag"]), str(metadata["anchor_tag"])
    entities = f"@e[tag={tag}]"
    anchor = f"@e[type=minecraft:marker,tag={anchor_tag},limit=1]"
    pixels = f"@e[type=minecraft:text_display,tag={tag}]"
    player = "@s[type=minecraft:player]"
    step = video.config.ticks_per_frame
    texture = metadata["renderer"] == "text_display_texture"

    def call(name: str) -> str:
        return f"function {namespace}:video/{name}"

    def score_set(name: str, value: int) -> str:
        return f"scoreboard players set #{name} {objective} {value}"

    def in_dimensions(command: str) -> list[str]:
        return [f"execute in {dimension} run {command}" for dimension in VIDEO_DIMENSIONS]

    clean = (
        f"execute as {entities} unless score @s {objective} = #vsession {objective} "
        "run kill @s"
    )
    video_functions: dict[str, list[str]] = {
        "clean": in_dimensions(clean),
        "screen_remove": [
            f"scoreboard players add #vsession {objective} 0",
            f"execute if score #vsession {objective} matches 2147483647 run {score_set('vsession', 0)}",
            f"scoreboard players add #vsession {objective} 1",
            score_set("vwanted", 0),
            score_set("vshown", -1),
            *in_dimensions(f"kill {entities}"),
        ],
        "screen_create": [f"execute if entity {player} at @s run {call('internal/check_dimension')}"],
        "internal/check_dimension": [
            score_set("vdimension", 0),
            *[
                f"execute if dimension {dimension} run {score_set('vdimension', 1)}"
                for dimension in VIDEO_DIMENSIONS
            ],
            f"execute if score #vdimension {objective} matches 1 run {call('internal/create')}",
            f"execute if score #vdimension {objective} matches 0 run tellraw @s "
            + json.dumps({"text": "Video screens support the Overworld, Nether and End only.", "color": "yellow"}),
        ],
        "internal/create": [
            call("screen_remove"),
            f"execute at @s rotated ~ 0 positioned ^ ^1.6 ^3 run {call('internal/create_here')}",
            call("render"),
        ],
        "internal/create_here": [
            "summon minecraft:marker ~ ~ ~ {Tags:" + json.dumps([tag, anchor_tag]) + "}",
            f"tp {anchor} ~ ~ ~ ~ 0",
            f"execute as {anchor} at @s run {call('internal/spawn_pixels')}",
        ],
        "internal/spawn_pixels": [],
        "ensure": [
            call("clean"),
            score_set("vexists", 0),
            *[
                f"execute in {dimension} if entity {anchor} run {score_set('vexists', 1)}"
                for dimension in VIDEO_DIMENSIONS
            ],
            f"execute if score #vexists {objective} matches 0 run {call('screen_create')}",
        ],
        "render": [
            call("clean"),
            f"scoreboard players operation #vframe {objective} = #frame {objective}",
            f"scoreboard players operation #vframe {objective} /= #vinterval {objective}",
            f"execute if score #vframe {objective} matches ..-1 run {score_set('vframe', 0)}",
            f"execute if score #vframe {objective} matches {video.frame_count}.. "
            f"run {score_set('vframe', video.frame_count - 1)}",
            f"scoreboard players operation #vwanted {objective} = #vframe {objective}",
            call("draw"),
        ],
        "watch": [
            f"execute unless score #vwanted {objective} = #vshown {objective} run {call('draw')}",
        ],
        "draw": [
            f"scoreboard players operation #vframe {objective} = #vwanted {objective}",
            *[
                f"execute in {dimension} if entity {anchor} run {call('internal/draw_here')}"
                for dimension in VIDEO_DIMENSIONS
            ],
        ],
        "internal/draw_here": [
            call("dispatch/root"),
            f"scoreboard players operation #vshown {objective} = #vframe {objective}",
        ],
    }
    rows = int(metadata["tile_rows"]) if texture else video.config.height
    columns = int(metadata["tile_columns"]) if texture else video.config.width
    for y in range(rows):
        check_cancelled(cancel_check)
        for x in range(columns):
            video_functions["internal/spawn_pixels"].append(
                "summon minecraft:text_display ~ ~ ~ " + (
                    _texture_nbt(x, y, namespace, metadata, pack_format) if texture else
                    _pixel_nbt(x, y, metadata, pack_format)
                )
            )
    video_functions["internal/spawn_pixels"].extend([
        f"execute as {pixels} at @s rotated as {anchor} run tp @s ~ ~ ~ ~180 0",
        f"scoreboard players operation {entities} {objective} = #vsession {objective}",
    ])

    for index, frame in enumerate(video.frames):
        check_cancelled(cancel_check)
        emit_progress(
            progress_callback, "datapack", index / max(1, video.frame_count),
            f"Writing video frame {index + 1}/{video.frame_count}",
        )
        lines = []
        if texture:
            for tile in range(int(metadata["display_entities"])):
                selector = f"@e[type=minecraft:text_display,tag={tag}_t{tile:04d},limit=1]"
                text = _component_nbt(tile_text_component(namespace, index, tile), pack_format)
                lines.append(f"data merge entity {selector} {{text:{text}}}")
        else:
            for pixel, rgb in enumerate(frame.reshape(-1, 3)):
                color = (int(rgb[0]) << 16) | (int(rgb[1]) << 8) | int(rgb[2])
                signed_argb = color - 16_777_216
                selector = f"@e[type=minecraft:text_display,tag={tag}_p{pixel:04d},limit=1]"
                lines.append(f"data merge entity {selector} {{background:{signed_argb}}}")
        _write_function(function_root, f"video/frame/{index:06d}", lines)

    def dispatch(low: int, high: int, name: str) -> None:
        check_cancelled(cancel_check)
        if high - low < 16:
            lines = [
                f"execute if score #vframe {objective} matches {index} run {call(f'frame/{index:06d}')}"
                for index in range(low, high + 1)
            ]
        else:
            midpoint = (low + high) // 2
            left, right = f"n_{low}_{midpoint}", f"n_{midpoint + 1}_{high}"
            lines = [
                f"execute if score #vframe {objective} matches {low}..{midpoint} run {call(f'dispatch/{left}')}",
                f"execute if score #vframe {objective} matches {midpoint + 1}..{high} run {call(f'dispatch/{right}')}",
            ]
            dispatch(low, midpoint, left)
            dispatch(midpoint + 1, high, right)
        _write_function(function_root, f"video/dispatch/{name}", lines)

    dispatch(0, video.frame_count - 1, "root")
    for name, lines in video_functions.items():
        _write_function(function_root, f"video/{name}", lines)

    functions["load"].extend([
        score_set("vinterval", step), call("screen_remove"),
    ])
    functions["stop"].append(call("screen_remove"))
    functions["tick"].insert(0, call("clean"))
    functions["tick"].insert(1, call("watch"))
    functions["internal/start"].extend([call("ensure"), call("render")])
    functions["internal/start_all"].extend([
        f"execute as @a[limit=1] at @s run {call('ensure')}", call("render"),
    ])
    functions["internal/resume"].append(call("render"))
    functions["internal/seek"].append(call("render"))
    functions["help"].append(
        "tellraw @s " + json.dumps({
            "text": f"Use /function {namespace}:video/screen_create to place a screen; video/screen_remove hides it.",
            "color": "gray",
        })
    )
    emit_progress(progress_callback, "datapack", 1.0, "Video functions complete")
    return metadata
