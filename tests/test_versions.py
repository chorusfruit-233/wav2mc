from wav2mc.cli import build_parser
from wav2mc.config import (
    DEFAULT_DATA_PACK_FORMAT,
    DEFAULT_MINECRAFT_VERSION,
    DEFAULT_RESOURCE_PACK_FORMAT,
)
from wav2mc.utils import pack_metadata


def test_defaults_target_minecraft_26_2() -> None:
    parser = build_parser()
    bank_args = parser.parse_args(["bank-build"])
    convert_args = parser.parse_args(["convert", "input.wav"])

    assert DEFAULT_MINECRAFT_VERSION == "26.2"
    assert DEFAULT_RESOURCE_PACK_FORMAT == 88.0
    assert DEFAULT_DATA_PACK_FORMAT == 107.1
    assert bank_args.pack_format == 88.0
    assert convert_args.data_pack_format == 107.1


def test_pack_metadata_adds_required_format_range() -> None:
    current = pack_metadata(88.0, "current")
    legacy = pack_metadata(64.0, "legacy")

    assert current["min_format"] == [88, 0]
    assert current["max_format"] == [88, 0]
    assert "min_format" not in legacy
    assert "max_format" not in legacy
