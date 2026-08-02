import threading
import time
from pathlib import Path

import tkinter as tk

import pytest

pytest.importorskip("tkinter")

from wav2mc import gui
from wav2mc.audio import AudioStreamInfo, MediaInfo
from wav2mc.cli import main
from wav2mc.config import DEFAULT_DEVICE_PACK_PROFILES
from wav2mc.gui_state import GuiSettings, save_gui_settings
from wav2mc.utils import TaskCancelled


def test_gui_modes_use_recommended_audio_configs() -> None:
    expected = {
        "voice": (80, 8000, 8, 172),
        "normal": (60, 12000, 12, 198),
        "high": (40, 16000, 16, 211),
        "experimental": (20, 20000, 16, 225),
    }

    assert tuple(expected) == DEFAULT_DEVICE_PACK_PROFILES
    for mode, values in expected.items():
        config = gui.mode_audio_config(mode)
        assert (
            config.min_frequency,
            config.max_frequency,
            config.phase_count,
            len(config.frequencies),
        ) == values


def test_gui_mode_summary_reports_quantized_high_frequency() -> None:
    summary = gui.mode_summary("high")

    assert "16000 Hz" in summary
    assert "最高频点 15840 Hz" in summary
    assert "最多 24 个正弦 + 6 个噪声 + 6 个瞬态" in summary


@pytest.mark.parametrize(
    ("gain_db", "expected"),
    ((-6.0, 0.501187), (0.0, 1.0), (6.0, 1.995262)),
)
def test_gui_converts_db_gain_to_linear_multiplier(
    gain_db: float,
    expected: float,
) -> None:
    assert gui.gain_multiplier_from_db(gain_db) == pytest.approx(expected)


def test_gui_mode_load_and_result_command() -> None:
    assert gui.mode_maximum_command_load("normal", stereo=False) == 560
    assert gui.mode_maximum_command_load("normal", stereo=True) == 1120
    assert gui.result_function_command({"song_namespace": "My Song!"}) == (
        "/function my_song:start"
    )


def test_gui_conversion_output_paths_use_safe_namespace(tmp_path: Path) -> None:
    outputs = gui.conversion_output_paths(tmp_path, "My Song!")

    assert outputs == {
        "data_pack": tmp_path / "my_song_datapack.zip",
        "preview": tmp_path / "my_song_preview.wav",
        "report": tmp_path / "my_song_analysis.json",
    }


def test_gui_cli_dispatches_initial_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.m4s"
    output_dir = tmp_path / "output"
    received: dict[str, Path | None] = {}

    def fake_launch_gui(
        initial_input: Path | None = None,
        output_dir: Path = Path("output"),
    ) -> None:
        received["input"] = initial_input
        received["output_dir"] = output_dir

    monkeypatch.setattr(gui, "launch_gui", fake_launch_gui)

    assert main(["gui", str(source), "--output-dir", str(output_dir)]) == 0
    assert received == {"input": source, "output_dir": output_dir}


def _tk_root() -> tk.Tk:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display is unavailable: {exc}")
    root.withdraw()
    return root


def test_gui_restores_settings_but_cli_output_wins(tmp_path: Path) -> None:
    settings_path = tmp_path / "gui.json"
    save_gui_settings(
        GuiSettings(
            output_dir="remembered",
            mode="high",
            gain_db=-3.5,
            advanced_open=True,
            selected_profiles=("voice", "high"),
        ),
        settings_path,
    )
    root = _tk_root()
    app = gui.Wav2McApp(
        root,
        initial_output_dir=tmp_path / "cli-output",
        settings_path=settings_path,
    )

    assert app.output_var.get() == str(tmp_path / "cli-output")
    assert app.mode_var.get() == "high"
    assert app.gain_db_var.get() == pytest.approx(-3.5)
    assert app.advanced_open is True
    assert app.profile_vars["voice"].get() is True
    assert app.profile_vars["normal"].get() is False
    app._destroy()


def test_gui_ignores_stale_probe_and_cancels_worker(tmp_path: Path) -> None:
    settings_path = tmp_path / "gui.json"
    root = _tk_root()
    app = gui.Wav2McApp(root, settings_path=settings_path)
    current = tmp_path / "current.m4s"
    stale = tmp_path / "stale.m4s"
    app.input_var.set(str(current))
    app.probe_generation = 2
    info = MediaInfo(
        duration=3.0,
        format_name="mov",
        streams=(AudioStreamInfo(0, "aac", 48_000, 2, 3.0),),
    )

    app._probe_succeeded(1, stale, info)
    assert app.media_info is None
    app._probe_succeeded(2, current, info)
    assert app.media_info == info
    assert "AAC" in app.media_info_var.get()

    started = threading.Event()

    def work(_progress: object, cancel_check: object) -> object:
        started.set()
        while not cancel_check():  # type: ignore[operator]
            time.sleep(0.005)
        raise TaskCancelled()

    app._run_task(
        "test",
        work,  # type: ignore[arg-type]
        lambda _result: None,
        app.convert_button,
        "开始转换",
    )
    assert started.wait(1.0)
    assert app.convert_button.cget("text") == "取消"
    app._request_cancel()
    deadline = time.monotonic() + 2.0
    while app.busy and time.monotonic() < deadline:
        app._poll_events()
        root.update()
        time.sleep(0.01)
    assert app.busy is False
    assert app.status_var.get() == "已取消"

    app.last_report = {"song_namespace": "demo_song"}
    app._copy_result_command()
    assert root.clipboard_get() == "/function demo_song:start"
    app._destroy()
