from __future__ import annotations

import math
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

import soundfile as sf


class PreviewPlayer:
    """Control a local preview without taking over the system's audio player.

    Pausing releases the audio device. Resuming and seeking start ffplay at the
    saved position, so these operations also work on Windows without signals.
    The displayed position uses a local clock and is approximate.
    """

    def __init__(
        self,
        *,
        executable: str | None = None,
        process_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.executable = executable or shutil.which("ffplay")
        self._process_factory = process_factory
        self._clock = clock
        self._process: subprocess.Popen[bytes] | None = None
        self._stderr: BinaryIO | None = None
        self._offset = 0.0
        self._started_at = 0.0
        self.path: Path | None = None
        self.duration = 0.0
        self.state = "stopped"
        self.error: str | None = None

    @property
    def available(self) -> bool:
        return self.executable is not None

    @property
    def position(self) -> float:
        elapsed = self._clock() - self._started_at if self.state == "playing" else 0.0
        return min(self.duration, max(0.0, self._offset + elapsed))

    def load(self, path: Path) -> None:
        self.stop()
        self.path = None
        self.duration = 0.0
        resolved = path.expanduser().resolve()
        info = sf.info(resolved)
        duration = float(info.duration)
        if not math.isfinite(duration) or duration <= 0.0:
            raise ValueError("预览音频为空，无法播放。")
        self.path = resolved
        self.duration = duration

    def play(self) -> None:
        self.poll()
        if self.state == "playing":
            return
        if self.path is None:
            raise ValueError("请先生成预览音频。")
        if not self.available:
            raise FileNotFoundError("找不到 ffplay，请将它加入 PATH。")
        if self._offset >= self.duration:
            self._offset = 0.0
        self.error = None
        self._stderr = tempfile.TemporaryFile(mode="w+b")
        options: dict[str, object] = {}
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self._process = self._process_factory(
                [
                    str(self.executable), "-nodisp", "-autoexit", "-loglevel", "error",
                    "-nostats", "-ss", f"{self._offset:.6f}", "-i", str(self.path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=self._stderr,
                **options,
            )
        except OSError:
            self._close_stderr()
            self.state = "paused" if self._offset else "stopped"
            raise
        self._started_at = self._clock()
        self.state = "playing"

    def pause(self) -> None:
        self.poll()
        if self.state != "playing":
            return
        self._offset = self.position
        self._stop_process()
        self.state = "paused"

    def seek(self, seconds: float) -> None:
        if not math.isfinite(seconds):
            raise ValueError("播放位置必须是有限数值。")
        self.poll()
        was_playing = self.state == "playing"
        self._stop_process()
        self._offset = min(self.duration, max(0.0, seconds))
        self.error = None
        self.state = "paused" if self._offset < self.duration else "finished"
        if was_playing and self._offset < self.duration:
            self.play()

    def stop(self) -> None:
        self._stop_process()
        self._offset = 0.0
        self.state = "stopped"
        self.error = None

    def poll(self) -> str:
        if self._process is None:
            return self.state
        return_code = self._process.poll()
        if return_code is None:
            return self.state
        self._offset = self.position
        self._process.wait()
        self._process = None
        reason = ""
        if self._stderr is not None:
            self._stderr.seek(0, 2)
            size = self._stderr.tell()
            self._stderr.seek(max(0, size - 2048))
            reason = self._stderr.read().decode("utf-8", errors="replace").strip()
        # ffplay can return zero for decoder and audio-device failures. At the
        # selected error log level, diagnostics still establish a failed play.
        if return_code or reason:
            self.error = "ffplay 播放失败"
            if return_code:
                self.error += f"（退出码 {return_code}）"
            if reason:
                self.error += f"：{reason}"
            self.state = "stopped"
        else:
            self._offset = self.duration
            self.state = "finished"
        self._close_stderr()
        return self.state

    def close(self) -> None:
        self.stop()

    def _stop_process(self) -> None:
        process = self._process
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1.0)
            else:
                process.wait()
            self._process = None
        self._close_stderr()

    def _close_stderr(self) -> None:
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None
