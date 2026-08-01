from pathlib import Path

import pytest

pytest.importorskip("tkinter")

from wav2mc import gui
from wav2mc.cli import main
from wav2mc.config import DEFAULT_DEVICE_PACK_PROFILES


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
    assert "每帧最多 24 个分量" in summary


@pytest.mark.parametrize(
    ("gain_db", "expected"),
    ((-6.0, 0.501187), (0.0, 1.0), (6.0, 1.995262)),
)
def test_gui_converts_db_gain_to_linear_multiplier(
    gain_db: float,
    expected: float,
) -> None:
    assert gui.gain_multiplier_from_db(gain_db) == pytest.approx(expected)


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
