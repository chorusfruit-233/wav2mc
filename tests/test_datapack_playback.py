from __future__ import annotations

import json
import math
import re
from pathlib import Path
from zipfile import ZipFile

import pytest

from wav2mc.analysis import AudioFrame, Component, ResidualComponent
from wav2mc.config import DEFAULT_DATA_PACK_FORMAT
from wav2mc.datapack import build_data_pack


class PlaybackWorld:
    """Execute the scoreboard/selector subset emitted by the player generator.

    Running the actual generated functions catches ordering errors which string
    assertions cannot detect, including looping twice or advancing while paused.
    This deliberately does not simulate Minecraft's audio mixer or networking.
    """

    def __init__(self) -> None:
        self.players = {"alice", "bob"}
        self.tags: dict[str, set[str]] = {name: set() for name in self.players}
        self.scores: dict[tuple[str, str], int] = {}
        self.functions: dict[str, list[str]] = {}
        self.metadata: dict[str, dict] = {}
        self.frames: list[tuple[str, int]] = []
        self.sounds: list[tuple[str, str]] = []
        self.stops: list[tuple[str, str, str]] = []
        self.messages: list[tuple[str, object]] = []

    def install(self, path: Path) -> dict:
        with ZipFile(path) as archive:
            metadata = json.loads(archive.read("wav2mc-song.json"))
            namespace = metadata["namespace"]
            self.metadata[namespace] = metadata["playback"]
            for name in archive.namelist():
                match = re.fullmatch(
                    r"data/([^/]+)/functions?/(.+)\.mcfunction", name,
                )
                if match:
                    key = f"{match[1]}:{match[2]}"
                    self.functions[key] = archive.read(name).decode().splitlines()
        self.run(f"{namespace}:load")
        return self.metadata[namespace]

    def score(self, namespace: str, holder: str) -> int:
        objective = self.metadata[namespace]["objective"]
        return self.scores[holder, objective]

    def set_score(self, namespace: str, holder: str, value: int) -> None:
        self.scores[holder, self.metadata[namespace]["objective"]] = value

    def listeners(self, namespace: str) -> set[str]:
        tag = self.metadata[namespace]["listener_tag"]
        return {name for name in self.players if tag in self.tags[name]}

    def tick(self, namespace: str, count: int = 1) -> None:
        for _ in range(count):
            self.run(f"{namespace}:tick")

    def tick_all(self, count: int = 1) -> None:
        for _ in range(count):
            for namespace in self.metadata:
                self.run(f"{namespace}:tick")

    def run(self, function: str, executor: str | None = None) -> None:
        assert function in self.functions, f"Missing function: {function}"
        match = re.fullmatch(r"([^:]+):frame/(\d+)", function)
        if match:
            self.frames.append((match[1], int(match[2])))
        for command in self.functions[function]:
            if command.strip() and not command.startswith("#"):
                self.command(command, executor)

    def select(self, selector: str, executor: str | None) -> list[str]:
        if selector.startswith("@s"):
            players = [executor] if executor in self.players else []
        elif selector.startswith("@a") or selector.startswith("@p"):
            players = sorted(self.players)
        else:
            return [selector] if selector in self.players else []
        if "[" in selector:
            options = selector.partition("[")[2].rstrip("]").split(",")
            for option in options:
                key, _, value = option.partition("=")
                if key == "tag":
                    negate = value.startswith("!")
                    tag = value.lstrip("!")
                    players = [
                        player for player in players
                        if (tag in self.tags[player]) != negate
                    ]
                elif key == "type":
                    is_player = value in ("player", "minecraft:player")
                    if not is_player:
                        players = []
                elif key == "limit":
                    players = players[:int(value)]
                else:
                    raise AssertionError(f"Unsupported selector option: {option}")
        return players[:1] if selector.startswith("@p") else players

    @staticmethod
    def _matches(value: int, spec: str) -> bool:
        if ".." not in spec:
            return value == int(spec)
        lower, upper = spec.split("..")
        return (not lower or value >= int(lower)) and (
            not upper or value <= int(upper)
        )

    def _holder(self, holder: str, executor: str | None) -> str:
        return executor if holder == "@s" and executor is not None else holder

    def _key(self, holder: str, objective: str, executor: str | None) -> tuple[str, str]:
        return self._holder(holder, executor), objective

    def _execute(self, tokens: list[str], executor: str | None) -> None:
        index = 0
        contexts = [executor]
        while index < len(tokens):
            token = tokens[index]
            if token == "run":
                for context in contexts:
                    self.command(" ".join(tokens[index + 1:]), context)
                return
            if token == "as":
                contexts = [
                    player for context in contexts
                    for player in self.select(tokens[index + 1], context)
                ]
                index += 2
            elif token in ("at", "anchored"):
                index += 2
            elif token in ("if", "unless"):
                negate = token == "unless"
                kind = tokens[index + 1]
                if kind == "entity":
                    selector = tokens[index + 2]
                    contexts = [
                        context for context in contexts
                        if bool(self.select(selector, context)) != negate
                    ]
                    index += 3
                elif kind == "score":
                    holder, objective, operator = tokens[index + 2:index + 5]
                    if operator == "matches":
                        spec = tokens[index + 5]

                        def condition(context: str | None) -> bool:
                            value = self.scores.get(self._key(holder, objective, context))
                            return value is not None and self._matches(value, spec)

                        index += 6
                    else:
                        other, other_objective = tokens[index + 5:index + 7]

                        def condition(context: str | None) -> bool:
                            value = self.scores.get(self._key(holder, objective, context))
                            rhs = self.scores.get(self._key(other, other_objective, context))
                            if value is None or rhs is None:
                                return False
                            return {
                                "=": value == rhs,
                                "<": value < rhs,
                                ">": value > rhs,
                                "<=": value <= rhs,
                                ">=": value >= rhs,
                            }[operator]

                        index += 7
                    contexts = [context for context in contexts if condition(context) != negate]
                else:
                    raise AssertionError(f"Unsupported execute condition: {kind}")
            else:
                raise AssertionError(f"Unsupported execute token: {token}")
        raise AssertionError("execute without run")

    def command(self, command: str, executor: str | None) -> None:
        tokens = command.split()
        if tokens[0] == "execute":
            self._execute(tokens[1:], executor)
        elif tokens[0] == "function":
            self.run(tokens[1], executor)
        elif tokens[0] == "tag":
            for player in self.select(tokens[1], executor):
                if tokens[2] == "add":
                    self.tags[player].add(tokens[3])
                elif tokens[2] == "remove":
                    self.tags[player].discard(tokens[3])
                else:
                    raise AssertionError(command)
        elif tokens[:2] == ["scoreboard", "objectives"]:
            assert tokens[2] == "add"
        elif tokens[:2] == ["scoreboard", "players"]:
            action, holder, objective = tokens[2:5]
            targets = self.select(holder, executor) if holder.startswith("@") else [holder]
            for target in targets:
                key = self._key(target, objective, executor)
                if action == "set":
                    value = int(tokens[5])
                elif action in ("add", "remove"):
                    sign = 1 if action == "add" else -1
                    value = self.scores.get(key, 0) + sign * int(tokens[5])
                elif action == "operation":
                    operator, other, other_objective = tokens[5:8]
                    rhs = self.scores[self._key(other, other_objective, executor)]
                    lhs = self.scores.get(key, 0)
                    if operator == "=":
                        value = rhs
                    elif operator == "+=":
                        value = lhs + rhs
                    elif operator == "-=":
                        value = lhs - rhs
                    elif operator == "*=":
                        value = lhs * rhs
                    elif operator == "/=":
                        value = math.trunc(lhs / rhs)
                    elif operator == "%=":
                        value = lhs % rhs
                    elif operator == "<":
                        value = min(lhs, rhs)
                    elif operator == ">":
                        value = max(lhs, rhs)
                    else:
                        raise AssertionError(command)
                else:
                    raise AssertionError(command)
                self.scores[key] = (value + 2**31) % 2**32 - 2**31
        elif tokens[0] == "playsound":
            self.sounds.extend((player, tokens[1]) for player in self.select(tokens[3], executor))
        elif tokens[0] == "stopsound":
            assert len(tokens) == 4, "Stopping an entire sound category is too broad"
            self.stops.extend(
                (player, tokens[2], tokens[3])
                for player in self.select(tokens[1], executor)
            )
        elif tokens[0] == "tellraw":
            content = json.loads(command.split(" ", 2)[2])
            self.messages.extend(
                (player, content) for player in self.select(tokens[1], executor)
            )
        else:
            raise AssertionError(f"Unsupported Minecraft command: {command}")


def make_pack(
    tmp_path: Path,
    namespace: str = "song",
    frame_count: int = 3,
    layout: str = "modern",
) -> Path:
    target = tmp_path / f"{namespace}.zip"
    frames = [
        AudioFrame(index, (Component(440 + 20 * index, 0, 0.2),))
        for index in range(frame_count)
    ]
    build_data_pack(
        target, frames, namespace, "wav2mc", DEFAULT_DATA_PACK_FORMAT, layout,
    )
    return target


def installed_world(tmp_path: Path, frame_count: int = 3) -> PlaybackWorld:
    world = PlaybackWorld()
    world.install(make_pack(tmp_path, frame_count=frame_count))
    return world


@pytest.mark.parametrize("layout", ["modern", "legacy"])
def test_playback_metadata_matches_pack_commands(tmp_path: Path, layout: str) -> None:
    world = PlaybackWorld()
    metadata = world.install(make_pack(tmp_path, frame_count=25, layout=layout))
    assert len(metadata["objective"]) <= 16
    assert re.fullmatch(r"[a-z0-9_]+", metadata["objective"])
    assert metadata["tick_rate"] == 20
    assert metadata["duration_ticks"] == 25
    assert metadata["duration_seconds"] == 1.25
    assert metadata["seek_step_seconds"] == 10
    for name in (
        "start", "start_all", "join", "leave", "pause", "resume", "stop",
        "loop_on", "loop_off", "seek", "seek_forward", "seek_backward", "status",
    ):
        assert metadata["commands"][name] == f"/function song:{name}"
        assert f"song:{name}" in world.functions


def test_pause_resume_and_finish_preserve_position_and_natural_tail(tmp_path: Path) -> None:
    world = installed_world(tmp_path)
    world.run("song:start", "alice")
    world.tick("song")
    assert world.frames == [("song", 0)]
    assert world.score("song", "#frame") == 1
    world.run("song:pause", "alice")
    stops_after_pause = len(world.stops)
    world.tick("song", 3)
    assert world.frames == [("song", 0)]
    assert world.score("song", "#frame") == 1
    assert world.score("song", "#playing") == 2
    world.run("song:resume", "alice")
    world.tick("song", 2)
    assert world.frames == [("song", 0), ("song", 1), ("song", 2)]
    assert world.score("song", "#playing") == 0
    assert world.score("song", "#frame") == 3
    assert len(world.stops) == stops_after_pause
    world.tick("song", 2)
    assert len(world.frames) == 3


def test_loop_restarts_on_next_tick_without_skipping_or_duplicate_frames(tmp_path: Path) -> None:
    world = installed_world(tmp_path)
    world.run("song:loop_on", "alice")
    world.run("song:start", "alice")
    world.tick("song", 7)
    assert world.frames == [("song", index) for index in (0, 1, 2, 0, 1, 2, 0)]
    assert world.score("song", "#playing") == 1
    world.run("song:loop_off", "alice")
    world.tick("song", 2)
    assert world.score("song", "#playing") == 0
    assert len(world.frames) == 9


def test_installed_songs_do_not_share_state_or_listeners(tmp_path: Path) -> None:
    world = installed_world(tmp_path)
    second = world.install(make_pack(tmp_path, namespace="other"))
    assert second["objective"] != world.metadata["song"]["objective"]
    assert second["listener_tag"] != world.metadata["song"]["listener_tag"]
    world.run("song:start", "alice")
    world.tick("song")
    world.run("other:start", "bob")
    world.tick("other")
    assert world.listeners("song") == {"alice"}
    assert world.listeners("other") == {"bob"}
    assert world.score("song", "#frame") == world.score("other", "#frame") == 1
    world.run("song:stop", "alice")
    assert world.score("song", "#playing") == 0
    assert world.score("other", "#playing") == 1
    assert world.listeners("other") == {"bob"}
    world.tick("other")
    assert world.frames[-1] == ("other", 1)


def test_shared_world_ticks_keep_song_positions_independent(tmp_path: Path) -> None:
    world = installed_world(tmp_path)
    world.install(make_pack(tmp_path, namespace="other"))
    world.run("song:start", "alice")
    world.tick_all()
    world.run("other:start", "bob")
    world.tick_all()
    world.run("song:pause", "alice")
    world.tick_all(2)
    assert world.frames == [
        ("song", 0), ("song", 1), ("other", 0), ("other", 1), ("other", 2),
    ]
    assert world.score("song", "#frame") == 2
    assert world.score("song", "#playing") == 2
    assert world.score("other", "#frame") == 3
    assert world.score("other", "#playing") == 0
    world.run("song:resume", "alice")
    world.tick_all()
    assert world.frames[-1] == ("song", 2)
    assert world.score("song", "#playing") == 0
    assert world.score("other", "#frame") == 3


def test_console_start_does_not_reset_an_existing_session(tmp_path: Path) -> None:
    world = installed_world(tmp_path)
    world.run("song:start", "alice")
    world.tick("song")
    scores = dict(world.scores)
    tags = {name: set(tags) for name, tags in world.tags.items()}
    stops = list(world.stops)
    world.run("song:start")
    assert world.scores == scores
    assert world.tags == tags
    assert world.stops == stops


def test_join_leave_sync_listeners_without_restarting_song(tmp_path: Path) -> None:
    world = installed_world(tmp_path)
    world.run("song:start", "alice")
    world.tick("song")
    world.run("song:join", "bob")
    assert world.score("song", "#frame") == 1
    world.tick("song")
    event = "wav2mc:grain.f0460.p00"
    assert ("alice", event) in world.sounds
    assert ("bob", event) in world.sounds
    world.run("song:leave", "alice")
    assert world.listeners("song") == {"bob"}
    world.run("song:leave", "bob")
    assert not world.listeners("song")
    assert world.score("song", "#playing") == 2
    assert world.score("song", "#frame") == 2
    world.tick("song", 2)
    assert world.frames == [("song", 0), ("song", 1)]
    world.run("song:join", "bob")
    world.run("song:resume", "bob")
    world.tick("song")
    assert world.frames[-1] == ("song", 2)


def test_join_before_start_then_resume_begins_at_first_frame(tmp_path: Path) -> None:
    world = installed_world(tmp_path)
    world.run("song:join", "alice")
    world.run("song:join", "bob")
    assert world.listeners("song") == {"alice", "bob"}
    world.tick_all(2)
    assert world.score("song", "#playing") == 0
    assert not world.frames
    world.run("song:resume", "alice")
    world.tick_all()
    assert world.frames == [("song", 0)]
    assert {player for player, _ in world.sounds} == {"alice", "bob"}


def test_start_all_supports_console_and_disconnect_pauses_timeline(tmp_path: Path) -> None:
    world = installed_world(tmp_path)
    world.run("song:start_all")
    assert world.listeners("song") == {"alice", "bob"}
    world.tick("song")
    world.players.clear()
    world.tick("song", 2)
    assert world.score("song", "#playing") == 2
    assert world.score("song", "#frame") == 1
    assert world.frames == [("song", 0)]


@pytest.mark.parametrize("reset", ["start", "start_all", "stop", "load"])
def test_offline_listener_does_not_rejoin_a_new_session_implicitly(
    tmp_path: Path, reset: str,
) -> None:
    world = installed_world(tmp_path)
    world.run("song:start", "alice")
    world.tick("song")
    world.players.remove("alice")
    world.run(f"song:{reset}", "bob")
    world.players.add("alice")
    sounds_before_return = len(world.sounds)
    world.tick("song")
    expected_listeners = {"bob"} if reset in ("start", "start_all") else set()
    assert world.listeners("song") == expected_listeners
    assert {player for player, _ in world.sounds[sounds_before_return:]} == expected_listeners


def test_single_frame_loop_and_resume_after_finishing(tmp_path: Path) -> None:
    world = installed_world(tmp_path, frame_count=1)
    world.run("song:loop_on")
    world.run("song:start", "alice")
    world.tick("song", 3)
    assert world.frames == [("song", 0)] * 3
    world.run("song:loop_off")
    world.tick("song")
    assert world.score("song", "#playing") == 0
    world.run("song:resume", "alice")
    world.tick("song")
    assert world.frames == [("song", 0)] * 5


def test_status_and_help_produce_valid_messages_without_advancing_playback(tmp_path: Path) -> None:
    world = installed_world(tmp_path, frame_count=25)
    world.run("song:start", "alice")
    world.tick("song", 21)
    before = (
        world.score("song", "#frame"),
        world.score("song", "#playing"),
        world.listeners("song"),
    )
    world.run("song:status", "alice")
    world.run("song:help", "alice")
    assert world.score("song", "#seconds") == 1
    assert before == (
        world.score("song", "#frame"),
        world.score("song", "#playing"),
        world.listeners("song"),
    )
    assert all(player == "alice" for player, _ in world.messages)
    assert any("seek" in json.dumps(message) for _, message in world.messages)


@pytest.mark.parametrize("seconds,expected", [(-2**31, 0), (-1, 0), (1, 20), (2**31 - 1, 405)])
def test_seek_clamps_before_scoreboard_multiplication(
    tmp_path: Path, seconds: int, expected: int,
) -> None:
    world = installed_world(tmp_path, frame_count=405)
    world.run("song:start", "alice")
    world.run("song:pause", "alice")
    world.set_score("song", "#seek", seconds)
    world.run("song:seek", "alice")
    assert world.score("song", "#frame") == expected
    assert world.score("song", "#playing") == (0 if expected == 405 else 2)
    assert not world.frames


def test_seek_forward_backward_preserve_subsecond_position_and_clamp(tmp_path: Path) -> None:
    world = installed_world(tmp_path, frame_count=405)
    world.run("song:start", "alice")
    world.tick("song", 3)
    world.run("song:seek_forward", "alice")
    assert world.score("song", "#frame") == 203
    world.run("song:seek_backward", "alice")
    assert world.score("song", "#frame") == 3
    world.run("song:seek_backward", "alice")
    assert world.score("song", "#frame") == 0
    world.run("song:seek_forward", "alice")
    world.tick("song")
    assert world.frames[-1] == ("song", 200)


def test_stop_and_reload_reset_only_the_song_session(tmp_path: Path) -> None:
    world = installed_world(tmp_path)
    world.install(make_pack(tmp_path, namespace="other"))
    world.run("song:start_all")
    world.run("other:start", "bob")
    world.tick("song")
    world.run("song:loop_on")
    world.run("song:stop")
    assert world.score("song", "#frame") == 0
    assert world.score("song", "#playing") == 0
    assert not world.listeners("song")
    world.run("song:start", "alice")
    world.tick("song")
    world.run("song:load")
    assert world.score("song", "#frame") == 0
    assert world.score("song", "#playing") == 0
    assert world.score("song", "#loop") == 0
    assert not world.listeners("song")
    assert world.score("other", "#playing") == 1
    assert world.listeners("other") == {"bob"}


def test_empty_song_never_enters_playing_state(tmp_path: Path) -> None:
    world = installed_world(tmp_path, frame_count=0)
    for function in ("start", "start_all", "join", "resume", "loop_on"):
        world.run(f"song:{function}", "alice")
    world.tick("song", 3)
    assert world.score("song", "#playing") == 0
    assert not world.frames


def test_noncontiguous_frame_indices_are_rejected_before_writing(tmp_path: Path) -> None:
    target = tmp_path / "invalid.zip"
    with pytest.raises(ValueError, match="frame|indices|contiguous"):
        build_data_pack(
            target, [AudioFrame(1, ())], "song", "wav2mc",
            DEFAULT_DATA_PACK_FORMAT, "modern",
        )
    assert not target.exists()


def test_stopping_song_targets_only_its_used_sound_events(tmp_path: Path) -> None:
    target = tmp_path / "song.zip"
    build_data_pack(
        target,
        [AudioFrame(0, (Component(440, 0, 0.2),), (
            ResidualComponent("noise", 2, 100, 200, 0, 0.1),
            ResidualComponent("transient", 3, 200, 400, 1, 0.1, delay_ms=10, shape="fast"),
        ))],
        "song", "wav2mc", DEFAULT_DATA_PACK_FORMAT, "modern",
    )
    world = PlaybackWorld()
    world.install(target)
    world.run("song:start", "alice")
    world.tick("song")
    world.stops.clear()
    world.run("song:stop", "alice")
    assert {player for player, _, _ in world.stops} == {"alice"}
    assert len({event for _, event in world.sounds}) == 3
    assert {event for _, _, event in world.stops} == {event for _, event in world.sounds}
    assert all(category == "record" for _, category, _ in world.stops)
