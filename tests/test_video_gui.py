import json
import wave
from pathlib import Path

import pytest

from wav2mc.config import VIDEO_QUALITY_PROFILES, VideoConfig, video_config_for_quality
from wav2mc.gui_state import GuiSettings, load_gui_settings, save_gui_settings


def test_video_settings_round_trip_preserves_existing_settings_version(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    settings = GuiSettings(
        mode="high", video_enabled=True, video_width=24, video_height=16,
        video_fps=5, video_pixel_size=0.2, video_stream=1, include_audio=False,
    )
    save_gui_settings(settings, target)
    assert load_gui_settings(target) == settings
    assert json.loads(target.read_text())["version"] == 1
    target.write_text(json.dumps({"version": 1, "mode": "voice"}))
    restored = load_gui_settings(target)
    assert restored.mode == "voice"
    assert restored.video_enabled is False
    assert restored.include_audio is True
    assert (restored.video_width, restored.video_height, restored.video_fps) == (160, 90, 10)


@pytest.mark.parametrize("invalid", [
    {"video_width": -1}, {"video_width": 128, "video_height": 128, "video_renderer": "pixels"},
    {"video_fps": 12}, {"video_pixel_size": float("nan")},
    {"video_pixel_size": "invalid"},
])
def test_invalid_saved_video_config_falls_back_without_losing_audio_settings(
    tmp_path: Path, invalid: dict,
) -> None:
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({
        "version": 1, "mode": "high", "gain_db": -4,
        "video_enabled": True, "video_width": 24, **invalid,
        "video_stream": -1, "include_audio": "no",
    }))
    restored = load_gui_settings(target)
    assert restored.mode == "high" and restored.gain_db == -4
    assert restored.video_enabled is True
    assert restored.video_width == 160 and restored.video_height == 90
    assert restored.video_fps == 10 and restored.video_pixel_size == 0.02
    assert restored.video_stream == 0 and restored.include_audio is True


def _app(tmp_path: Path):
    tk = pytest.importorskip("tkinter")
    from wav2mc import gui
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(str(exc))
    root.withdraw()
    return gui.Wav2McApp(root, settings_path=tmp_path / "settings.json")


def test_video_gui_submits_validated_settings_and_keeps_audio_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wav2mc import gui
    app = _app(tmp_path)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"fake media; conversion backend is mocked")
    app.input_var.set(str(source))
    app.output_var.set(str(tmp_path / "output"))
    received = []
    monkeypatch.setattr(gui, "convert_audio", lambda **kwargs: received.append(kwargs) or {})
    monkeypatch.setattr(
        app, "_run_task",
        lambda _title, work, *_rest: work(lambda _progress: None, lambda: False),
    )
    try:
        app.video_enabled_var.set(True)
        app.video_width_var.set("24")
        app.video_height_var.set("16")
        app.video_fps_var.set("5")
        app.video_pixel_size_var.set("0.2")
        app.video_stream_var.set("1")
        app.include_audio_var.set(False)
        app._start_conversion()
        assert received[-1]["video_config"] == VideoConfig(24, 16, 5, 0.2)
        assert received[-1]["video_stream"] == 1
        assert received[-1]["include_audio"] is False
        assert "384" in app.video_summary_var.get()
        app.video_width_var.set("128")
        app.video_height_var.set("128")
        app.video_renderer_var.set("pixels")
        app._start_conversion()
        assert len(received) == 1
        assert app.convert_error_var.get().startswith("参数无效")
        app.video_enabled_var.set(False)
        app._start_conversion()
        assert received[-1]["video_config"] is None
        assert received[-1]["include_audio"] is True
        assert len(received) == 2
    finally:
        app._destroy()


def test_video_gui_restores_settings_and_copies_screen_commands(tmp_path: Path) -> None:
    import tkinter as tk
    settings = GuiSettings(
        video_enabled=True, video_width=16, video_height=12,
        video_fps=4, video_pixel_size=0.25, include_audio=False,
    )
    save_gui_settings(settings, tmp_path / "settings.json")
    app = _app(tmp_path)
    try:
        assert app._video_config() == VideoConfig(16, 12, 4, 0.25)
        assert app.include_audio_var.get() is False
        preview = tmp_path / "preview.wav"
        with wave.open(str(preview), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(100)
            output.writeframes(b"\x00\x00" * 100)
        report = tmp_path / "report.json"
        report.write_text(json.dumps({
            "song_namespace": "song", "audio_enabled": False,
            "required_resource_pack": None,
            "video": {"width": 16, "height": 12, "fps": 4},
            "playback": {"commands": {
                "start": "/function song:start",
                "screen_create": "/function song:video/screen_create",
                "screen_remove": "/function song:video/screen_remove",
            }},
        }))
        app._conversion_finished({"preview": preview, "report": report})
        assert app.command_menu.index(tk.END) == 2
        app.command_menu.invoke(1)
        assert app.root.clipboard_get() == "/function song:video/screen_create"
        app.command_menu.invoke(2)
        assert app.root.clipboard_get() == "/function song:video/screen_remove"
        assert "16×12" in app.result_video_var.get()
        assert app.result_pack_var.get() == "无声视频 · 无需资源包"
    finally:
        app._destroy()


def test_silent_video_probe_is_successful_and_conversion_remains_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wav2mc import gui
    from wav2mc.audio import AudioStreamInfo, MediaInfo
    from wav2mc.video import VideoInfo
    app = _app(tmp_path)
    source = tmp_path / "silent.mp4"
    source.write_bytes(b"fake video with mocked probe")
    app.input_var.set(str(source))
    app.output_var.set(str(tmp_path / "output"))
    logged = []
    received = []

    def no_audio(_path: Path):
        raise RuntimeError(f"No audio stream found in {source}")

    monkeypatch.setattr(gui, "probe_media", no_audio)
    monkeypatch.setattr(gui, "probe_video", lambda _path: VideoInfo(320, 180, 2.5, 0, 0, "h264"))
    monkeypatch.setattr(app, "_append_log", logged.append)
    monkeypatch.setattr(gui, "convert_audio", lambda **kwargs: received.append(kwargs) or {})
    monkeypatch.setattr(
        app, "_run_task",
        lambda _title, work, *_rest: work(lambda _progress: None, lambda: False),
    )
    try:
        app._begin_probe(source)
        event, generation, path, info = app.event_queue.get(timeout=1)
        assert event == "probe_success"
        assert isinstance(info, MediaInfo) and info.streams == ()
        app._probe_succeeded(generation, path, info)
        assert not logged
        assert app.video_enabled_var.get() is False
        assert "无音轨" in app.media_info_var.get()
        assert "启用视频转换" in app.media_info_var.get()
        assert str(app.stream_box.cget("state")) == "disabled"
        app._start_conversion()
        assert not received
        assert "开启视频" in app.convert_error_var.get()
        app.video_enabled_var.set(True)
        assert "无声视频" in app.media_info_var.get()
        app._start_conversion()
        assert received[0]["video_config"] == video_config_for_quality()
        assert not app.convert_error_var.get()
        audio_info = MediaInfo(2.5, "mp4", (AudioStreamInfo(0, "aac", 48000, 2, 2.5),))
        app._probe_succeeded(generation, source, audio_info)
        assert str(app.stream_box.cget("state")) == "readonly"
        assert app.audio_stream_var.get() == 0
    finally:
        app._destroy()


@pytest.mark.parametrize("saved_quality", [None, "low", "unknown", [], {}])
def test_legacy_and_inconsistent_video_quality_preserve_manual_settings(
    tmp_path: Path, saved_quality: object,
) -> None:
    target = tmp_path / "settings.json"
    payload = {
        "version": 1, "video_width": 24, "video_height": 16,
        "video_fps": 5, "video_pixel_size": 0.25,
    }
    if saved_quality is not None:
        payload["video_quality"] = saved_quality
    target.write_text(json.dumps(payload))
    settings = load_gui_settings(target)
    assert settings.video_quality == "custom"
    assert (
        settings.video_width, settings.video_height,
        settings.video_fps, settings.video_pixel_size,
    ) == (24, 16, 5, 0.25)


def test_saved_video_quality_infers_old_presets_and_keeps_explicit_custom(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({
        "version": 1, "video_width": 16, "video_height": 9,
        "video_fps": 5, "video_pixel_size": 0.3,
    }))
    assert load_gui_settings(target).video_quality == "custom"
    assert load_gui_settings(target).video_width == 16
    custom = GuiSettings(video_quality="custom")
    save_gui_settings(custom, target)
    assert load_gui_settings(target) == custom


def test_gui_video_quality_presets_apply_atomically_and_preserve_audio_mode(tmp_path: Path) -> None:
    app = _app(tmp_path)
    try:
        app.video_enabled_var.set(True)
        app.mode_var.set("voice")
        for quality, profile in VIDEO_QUALITY_PROFILES.items():
            app.video_quality_display_var.set(profile.label)
            app._select_video_quality(None)
            assert app.video_quality_var.get() == quality
            assert app.video_quality_display_var.get() == profile.label
            assert app._video_config() == video_config_for_quality(quality)
            assert app.mode_var.get() == "voice"
            if quality == "native":
                assert "跟随源视频" in app.video_summary_var.get()
            else:
                displays = {"low": 2304, "normal": 1, "high": 2, "ultra": 6}[quality]
                assert f"{displays * profile.fps:,} 次更新/秒" in app.video_summary_var.get()
        app.video_quality_var.set("ultra")
        app.video_pixel_size_var.set("0.2")
        assert app.video_quality_var.get() == "ultra"
        assert app._video_config().pixel_size == 0.2
        app.video_width_var.set("48")
        assert app.video_quality_var.get() == "custom"
        assert app.video_quality_display_var.get() == "自定义"
        before = app._video_config()
        app.video_quality_var.set("custom")
        assert app._video_config() == before
        settings = app._current_settings()
        assert settings.video_quality == "custom"
        assert settings.mode == "voice"
    finally:
        app._destroy()


def test_gui_restores_preset_size_override_and_submits_selected_quality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wav2mc import gui
    save_gui_settings(GuiSettings(
        video_enabled=True, video_quality="high", video_width=48,
        video_height=27, video_fps=10, video_pixel_size=0.2,
    ), tmp_path / "settings.json")
    app = _app(tmp_path)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"fake media; conversion backend is mocked")
    app.input_var.set(str(source))
    app.output_var.set(str(tmp_path / "output"))
    received = []
    monkeypatch.setattr(gui, "convert_audio", lambda **kwargs: received.append(kwargs) or {})
    monkeypatch.setattr(
        app, "_run_task",
        lambda _title, work, *_rest: work(lambda _progress: None, lambda: False),
    )
    try:
        assert app.video_quality_var.get() == "custom"
        assert app._video_config() == VideoConfig(48, 27, 10, 0.2)
        app._start_conversion()
        assert received[-1]["video_config"] == VideoConfig(48, 27, 10, 0.2)
        app.video_quality_var.set("low")
        app._start_conversion()
        assert received[-1]["video_config"] == video_config_for_quality("low")
    finally:
        app._destroy()


def test_native_settings_round_trip_and_invalid_native_falls_back(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    settings = GuiSettings(video_enabled=True, video_quality="native")
    assert settings.video_width is None and settings.video_height is None
    assert settings.video_pixel_size is None and settings.video_fps == 20
    save_gui_settings(settings, target)
    assert load_gui_settings(target) == settings
    payload = json.loads(target.read_text())
    payload["video_fps"] = 12
    target.write_text(json.dumps(payload))
    restored = load_gui_settings(target)
    assert restored.video_quality == "normal"
    assert (restored.video_width, restored.video_height) == (160, 90)


def test_native_gui_tracks_source_resolution_aspect_and_rejects_stale_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wav2mc import gui
    from wav2mc.video import VideoInfo
    app = _app(tmp_path)
    source = tmp_path / "native.mkv"
    source.write_bytes(b"mocked native video")
    app.input_var.set(str(source))
    app.output_var.set(str(tmp_path / "output"))
    received = []
    monkeypatch.setattr(gui, "convert_audio", lambda **kwargs: received.append(kwargs) or {})
    monkeypatch.setattr(
        app, "_run_task",
        lambda _title, work, *_rest: work(lambda _progress: None, lambda: False),
    )
    try:
        app.video_enabled_var.set(True)
        app.video_quality_var.set("native")
        assert "跟随源视频" in app.video_summary_var.get()
        info = VideoInfo(1920, 1080, 2, 0, 0, "h264", sample_aspect_ratio=2)
        app._video_probe_succeeded(app.probe_generation - 1, source, info)
        assert app.video_info is None
        app._video_probe_succeeded(app.probe_generation, source, info)
        assert (app.video_width_var.get(), app.video_height_var.get()) == ("1920", "1080")
        assert "40 个显示实体" in app.video_summary_var.get()
        assert "3.2 × 0.9" in app.video_summary_var.get()
        assert "需视频资源包" in app.video_summary_var.get()
        assert all(str(widget.cget("state")) == "disabled" for widget in app.video_dimension_widgets)
        app.video_fps_var.set("5")
        assert app.video_quality_var.get() == "native"
        assert "200 次更新/秒" in app.video_summary_var.get()
        app._start_conversion()
        assert received[-1]["video_config"] == video_config_for_quality("native", fps=5)
        app.video_renderer_var.set("pixels")
        app._start_conversion()
        assert len(received) == 1
        assert "4096" in app.convert_error_var.get()
        app.video_renderer_var.set("auto")
        app.input_var.set(str(tmp_path / "different.mkv"))
        app._update_video_controls()
        assert "跟随源视频" in app.video_summary_var.get()
    finally:
        app._destroy()


def test_gui_checks_existing_texture_pack_before_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wav2mc import gui
    app = _app(tmp_path)
    source = tmp_path / "input.mkv"
    source.write_bytes(b"mocked video")
    output = tmp_path / "output"
    output.mkdir()
    existing = output / "input_video_resourcepack.zip"
    existing.write_bytes(b"keep this resource pack")
    app.input_var.set(str(source))
    app.output_var.set(str(output))
    app.video_enabled_var.set(True)
    app.video_quality_var.set("native")
    prompts = []
    called = []
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda title, text, **kwargs: prompts.append(text) or False)
    monkeypatch.setattr(app, "_run_task", lambda *args: called.append(args))
    try:
        app._start_conversion()
        assert len(prompts) == 1 and "1 个" in prompts[0]
        assert not called
        assert existing.read_bytes() == b"keep this resource pack"
    finally:
        app._destroy()
