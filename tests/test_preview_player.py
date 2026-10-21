import subprocess
import wave
from pathlib import Path

import pytest

from wav2mc.preview_player import PreviewPlayer


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.waited = False
        self.hang_on_terminate = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if not self.hang_on_terminate:
            self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("ffplay", timeout)
        self.waited = True
        return self.returncode


def make_player(tmp_path: Path) -> tuple[PreviewPlayer, list[float], list[tuple]]:
    path = tmp_path / "tone with spaces.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(100)
        output.writeframes(b"\x00\x00" * 1000)
    now = [100.0]
    spawned: list[tuple] = []

    def spawn(arguments: list[str], **options: object) -> FakeProcess:
        process = FakeProcess()
        spawned.append((process, arguments, options))
        return process

    player = PreviewPlayer(
        executable="ffplay",
        process_factory=spawn,  # type: ignore[arg-type]
        clock=lambda: now[0],
    )
    player.load(path)
    return player, now, spawned


def test_pause_resume_preserves_position_and_reaps_audio_process(tmp_path: Path) -> None:
    player, now, spawned = make_player(tmp_path)
    player.play()
    now[0] += 2.5
    assert player.position == pytest.approx(2.5)
    player.pause()
    assert player.state == "paused"
    assert spawned[0][0].terminated and spawned[0][0].waited
    assert spawned[0][2]["stderr"].closed
    now[0] += 20
    assert player.position == pytest.approx(2.5)
    player.play()
    arguments = spawned[1][1]
    assert arguments[arguments.index("-ss") + 1] == "2.500000"
    assert arguments[-1] == str(player.path)
    now[0] += 1
    assert player.position == pytest.approx(3.5)
    player.close()
    assert spawned[1][0].terminated and spawned[1][0].waited


def test_seek_clamps_and_restarts_only_active_playback(tmp_path: Path) -> None:
    player, now, spawned = make_player(tmp_path)
    player.seek(3)
    assert not spawned
    assert player.state == "paused" and player.position == 3
    player.play()
    player.seek(-10)
    assert spawned[0][0].terminated
    assert player.state == "playing" and player.position == 0
    assert len(spawned) == 2
    player.seek(100)
    assert spawned[1][0].terminated
    assert player.state == "finished" and player.position == player.duration
    assert len(spawned) == 2
    player.play()
    assert player.position == 0
    player.stop()
    assert player.position == 0 and player.state == "stopped"


def test_completion_can_be_replayed_and_error_is_exposed(tmp_path: Path) -> None:
    player, now, spawned = make_player(tmp_path)
    player.play()
    now[0] += player.duration
    spawned[0][0].returncode = 0
    assert player.poll() == "finished"
    assert player.position == player.duration
    assert spawned[0][0].waited
    player.play()
    assert player.position == 0
    spawned[1][2]["stderr"].write(b"no audio device")
    spawned[1][0].returncode = 1
    assert player.poll() == "stopped"
    assert "no audio device" in str(player.error)
    assert spawned[1][2]["stderr"].closed
    player.stop()
    assert player.error is None


def test_ffplay_zero_exit_with_error_diagnostics_is_not_success(tmp_path: Path) -> None:
    player, now, spawned = make_player(tmp_path)
    player.play()
    spawned[0][2]["stderr"].write(b"Could not initialize audio device")
    spawned[0][0].returncode = 0
    assert player.poll() == "stopped"
    assert "Could not initialize audio device" in str(player.error)
    assert player.position < player.duration
    assert spawned[0][0].waited and spawned[0][2]["stderr"].closed


def test_cleanup_kills_unresponsive_process_and_load_releases_previous_file(
    tmp_path: Path,
) -> None:
    player, now, spawned = make_player(tmp_path)
    player.play()
    spawned[0][0].hang_on_terminate = True
    player.load(player.path)  # type: ignore[arg-type]
    assert spawned[0][0].terminated and spawned[0][0].killed
    assert spawned[0][0].waited and spawned[0][2]["stderr"].closed
    assert player.state == "stopped" and player.position == 0
    player.close()
    player.close()


def test_start_failure_closes_error_file_and_allows_retry(tmp_path: Path) -> None:
    player, now, spawned = make_player(tmp_path)
    errors = []

    def fail(_args: object, **options: object) -> None:
        errors.append(options["stderr"])
        raise OSError("could not start ffplay")

    player._process_factory = fail  # type: ignore[assignment]
    with pytest.raises(OSError, match="could not start"):
        player.play()
    assert player.state == "stopped"
    assert errors[0].closed
    player.close()


def test_missing_ffplay_and_invalid_seek_do_not_start_process(tmp_path: Path) -> None:
    player, now, spawned = make_player(tmp_path)
    player.executable = None
    assert not player.available
    with pytest.raises(FileNotFoundError, match="ffplay"):
        player.play()
    for value in (float("nan"), float("inf"), -float("inf")):
        with pytest.raises(ValueError, match="有限"):
            player.seek(value)
    assert not spawned


def test_failed_load_stops_previous_audio_and_clears_stale_path(tmp_path: Path) -> None:
    player, now, spawned = make_player(tmp_path)
    player.play()
    with pytest.raises(RuntimeError):
        player.load(tmp_path / "missing.wav")
    assert spawned[0][0].terminated
    assert player.path is None
    assert player.duration == 0
    assert player.state == "stopped"
