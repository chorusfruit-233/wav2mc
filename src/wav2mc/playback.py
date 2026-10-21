from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable


TICK_RATE = 20
SEEK_STEP_SECONDS = 10
_SCORE_MAX = 2_147_483_647
_NAMESPACE = re.compile(r"[a-z0-9_.-]+\Z")
PLAYBACK_ACTIONS = (
    "start", "start_all", "join", "leave", "pause", "resume", "stop",
    "loop_on", "loop_off", "seek", "seek_forward", "seek_backward",
    "status", "help",
)


def playback_metadata(namespace: str, frame_count: int) -> dict[str, object]:
    """Describe the controls and isolated Minecraft state for one song."""
    if not _NAMESPACE.fullmatch(namespace):
        raise ValueError("namespace must be a valid Minecraft namespace")
    if not isinstance(frame_count, int) or not 0 <= frame_count <= _SCORE_MAX:
        raise ValueError("frame_count must fit a nonnegative Minecraft score")
    digest = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:12]
    objective = f"w2m_{digest}"
    return {
        "revision": 1,
        "objective": objective,
        "listener_tag": f"wav2mc_{digest}",
        "tick_rate": TICK_RATE,
        "duration_ticks": frame_count,
        "duration_seconds": frame_count / TICK_RATE,
        "seek_step_seconds": SEEK_STEP_SECONDS,
        "states": {"stopped": 0, "playing": 1, "paused": 2},
        "commands": {
            action: f"/function {namespace}:{action}"
            for action in PLAYBACK_ACTIONS
        },
        "seek_score": f"/scoreboard players set #seek {objective} <seconds>",
        "seek_command": f"/function {namespace}:seek",
        "seek_unit": "seconds",
        "stop_sound_scope": "used sound events only; shared events in other songs may also stop",
    }


def playback_functions(
    namespace: str,
    frame_count: int,
    sound_events: Iterable[str],
    category: str,
) -> dict[str, list[str]]:
    """Generate controls without macros so both directory layouts can use them."""
    metadata = playback_metadata(namespace, frame_count)
    objective = str(metadata["objective"])
    listener_tag = str(metadata["listener_tag"])
    listeners = f"@a[tag={listener_tag}]"
    player = "@s[type=minecraft:player]"
    step = SEEK_STEP_SECONDS * TICK_RATE
    functions: dict[str, list[str]] = {}

    def call(name: str) -> str:
        return f"function {namespace}:{name}"

    def set_score(name: str, value: int) -> str:
        return f"scoreboard players set #{name} {objective} {value}"

    def operation(target: str, operator: str, source: str) -> str:
        return (
            f"scoreboard players operation #{target} {objective} "
            f"{operator} #{source} {objective}"
        )

    def score(name: str, match: str, command: str) -> str:
        return f"execute if score #{name} {objective} matches {match} run {command}"

    def tell(text: str, color: str = "gray") -> str:
        return "tellraw @s " + json.dumps({"text": text, "color": color})

    def clean_then(*commands: str) -> list[str]:
        return [call("internal/clean"), *commands]

    def player_only(name: str) -> list[str]:
        return [f"execute if entity {player} run {call(name)}"]

    events = sorted(set(sound_events))
    functions["internal/halt"] = [
        f"stopsound {listeners} {category} {event}" for event in events
    ] or ["# No sound events"]
    functions["internal/halt_self"] = [
        f"stopsound @s {category} {event}" for event in events
    ] or ["# No sound events"]
    functions["internal/clean"] = [
        f"execute as {listeners} unless score @s {objective} = #session {objective} "
        f"run tag @s remove {listener_tag}",
    ]
    functions["internal/new_session"] = [
        f"scoreboard players add #session {objective} 0",
        score("session", str(_SCORE_MAX), set_score("session", 0)),
        f"scoreboard players add #session {objective} 1",
        f"tag @a remove {listener_tag}",
    ]
    functions["load"] = [
        f"scoreboard objectives add {objective} dummy",
        call("internal/clean"),
        call("internal/halt"),
        call("internal/new_session"),
        set_score("playing", 0),
        set_score("frame", 0),
        set_score("loop", 0),
        set_score("seek", 0),
        set_score("duration", frame_count),
        set_score("rate", TICK_RATE),
    ]
    functions["internal/start"] = clean_then(
        call("internal/halt"),
        call("internal/new_session"),
        f"tag @s add {listener_tag}",
        f"scoreboard players operation @s {objective} = #session {objective}",
        set_score("frame", 0),
        set_score("playing", 1),
        tell(f"Playing {namespace}", "green"),
    )
    functions["internal/start_all"] = clean_then(
        call("internal/halt"),
        call("internal/new_session"),
        f"tag @a add {listener_tag}",
        f"scoreboard players operation @a {objective} = #session {objective}",
        set_score("frame", 0),
        set_score("playing", 1),
    )
    if frame_count:
        functions["start"] = player_only("internal/start")
        functions["start_all"] = [
            f"execute if entity @a run {call('internal/start_all')}"
        ]
    else:
        functions["start"] = [tell("This song has no frames.", "yellow")]
        functions["start_all"] = ["# This song has no frames"]
    functions["join"] = player_only("internal/join")
    functions["internal/join"] = clean_then(
        f"tag @s add {listener_tag}",
        f"scoreboard players operation @s {objective} = #session {objective}",
        tell(f"Joined {namespace}. Use resume if playback is paused.", "green"),
    )
    functions["leave"] = player_only("internal/leave")
    functions["internal/leave"] = clean_then(
        f"execute if entity @s[tag={listener_tag}] run {call('internal/halt_self')}",
        f"tag @s remove {listener_tag}",
        f"execute unless entity {listeners} if score #playing {objective} matches 1 "
        f"run {call('pause')}",
    )
    functions["pause"] = clean_then(
        score("playing", "1", call("internal/pause")),
    )
    functions["internal/pause"] = [
        set_score("playing", 2),
        call("internal/halt"),
    ]
    functions["resume"] = clean_then(
        f"execute if entity {listeners} run {call('internal/resume')}"
    ) if frame_count else ["# This song has no frames"]
    functions["internal/resume"] = [
        score("frame", f"{frame_count}..", set_score("frame", 0)),
        set_score("playing", 1),
    ]
    functions["stop"] = clean_then(
        call("internal/halt"),
        call("internal/new_session"),
        set_score("playing", 0),
        set_score("frame", 0),
    )
    functions["finish"] = [
        set_score("playing", 0),
        set_score("frame", frame_count),
        "# Keep listeners and let the final grains decay naturally.",
    ]
    functions["loop_on"] = [set_score("loop", 1)]
    functions["loop_off"] = [set_score("loop", 0)]
    functions["seek"] = clean_then(
        score("seek", "..-1", set_score("seek", 0)),
        # Compare seconds before multiplying so even int32-max seeks are safe.
        score("seek", f"..{frame_count // TICK_RATE}",
              call("internal/seek_seconds")),
        score("seek", f"{frame_count // TICK_RATE + 1}..",
              set_score("frame", frame_count)),
        call("internal/seek"),
    )
    functions["internal/seek_seconds"] = [
        operation("frame", "=", "seek"),
        operation("frame", "*=", "rate"),
    ]
    functions["seek_forward"] = clean_then(
        operation("seek_tick", "=", "frame"),
        score("seek_tick", f"..{frame_count - step}",
              f"scoreboard players add #frame {objective} {step}"),
        score("seek_tick", f"{frame_count - step + 1}..",
              set_score("frame", frame_count)),
        call("internal/seek"),
    )
    functions["seek_backward"] = clean_then(
        f"scoreboard players remove #frame {objective} {step}",
        call("internal/seek"),
    )
    functions["internal/seek"] = [
        call("internal/halt"),
        score("frame", "..-1", set_score("frame", 0)),
        score("frame", f"{frame_count}..", call("finish")),
    ]
    if frame_count:
        functions["internal/seek"].append(
            f"execute if score #playing {objective} matches 0 "
            f"if score #frame {objective} matches ..{frame_count - 1} "
            f"if entity {listeners} run {set_score('playing', 2)}"
        )
    functions["tick"] = clean_then(
        f"execute unless entity {listeners} if score #playing {objective} matches 1 "
        f"run {call('pause')}",
        score("playing", "1", call("internal/tick")),
    )
    functions["internal/tick"] = [
        call("dispatch/root"),
        f"scoreboard players add #frame {objective} 1",
        score("frame", f"{frame_count}..", call("internal/end")),
    ]
    functions["internal/end"] = [
        score("loop", "0", call("finish")),
        score("loop", "1", set_score("frame", 0)),
    ]
    functions["status"] = player_only("internal/status")
    progress = [
        {"text": f"{namespace}: "},
        {"score": {"name": "#seconds", "objective": objective}},
        {"text": f" / {frame_count / TICK_RATE:g}s"},
    ]
    functions["internal/status"] = clean_then(
        operation("seconds", "=", "frame"),
        operation("seconds", "/=", "rate"),
        "tellraw @s " + json.dumps(progress),
        score("playing", "0", tell("Stopped")),
        score("playing", "1", tell("Playing", "green")),
        score("playing", "2", tell("Paused", "yellow")),
        score("loop", "1", tell("Loop: on")),
        score("loop", "0", tell("Loop: off")),
    )
    functions["help"] = [
        tell(f"/function {namespace}:<action>: start, start_all, pause, resume, stop, status"),
        tell(f"/function {namespace}:join or leave controls your listener membership."),
        tell(f"/function {namespace}:loop_on or loop_off toggles looping."),
        tell(f"/function {namespace}:seek_forward or seek_backward moves 10 seconds."),
        tell(f"Set seconds with /scoreboard players set #seek {objective} <seconds>"),
        tell(f"Then run /function {namespace}:seek."),
    ]
    return functions
