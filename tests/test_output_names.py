from pathlib import Path

import pytest

from wav2mc.utils import conversion_output_paths, safe_output_name, song_namespace


@pytest.mark.parametrize(("name", "expected"), [
    ("中文歌曲", "中文歌曲"),
    (" 歌曲 名称 .", "歌曲 名称"),
    ("../歌曲\\测试:音频?", "_歌曲_测试_音频_"),
    ("\x00", "_"),
    ("...", "song"),
    ("CON", "_CON"),
    ("nul.txt", "_nul.txt"),
])
def test_output_name_is_safe_and_preserves_unicode(name: str, expected: str) -> None:
    assert safe_output_name(name) == expected
    paths = conversion_output_paths(Path("output"), name)
    assert all(path.parent == Path("output") for path in paths.values())


def test_long_unicode_output_name_leaves_space_for_suffixes() -> None:
    paths = conversion_output_paths(Path("output"), "歌曲" * 100)
    assert all(len(path.name.encode("utf-8")) <= 255 for path in paths.values())


def test_chinese_song_names_have_distinct_stable_namespaces() -> None:
    assert song_namespace("中文歌曲") == song_namespace("中文歌曲")
    assert song_namespace("中文歌曲") != song_namespace("另一首歌曲")
    assert song_namespace("歌曲 A") != song_namespace("歌曲 B")
    assert song_namespace("My Song!") == "my_song"
