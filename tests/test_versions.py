import pytest

from wav2mc.cli import build_parser
from wav2mc.config import (
    DEFAULT_DATA_PACK_FORMAT,
    DEFAULT_MINECRAFT_VERSION,
    DEFAULT_RESOURCE_PACK_FORMAT,
)
from wav2mc.utils import pack_metadata


def test_defaults_target_minecraft_26_3() -> None:
    parser = build_parser()
    bank_args = parser.parse_args(["bank-build"])
    set_args = parser.parse_args(["bank-build-set"])
    convert_args = parser.parse_args(["convert", "input.wav"])

    assert DEFAULT_MINECRAFT_VERSION == "26.3"
    assert DEFAULT_RESOURCE_PACK_FORMAT == 97.1
    assert DEFAULT_DATA_PACK_FORMAT == 121.0
    assert bank_args.pack_format == 97.1
    assert set_args.pack_format == 97.1
    assert convert_args.data_pack_format == 121.0
    assert convert_args.layout == "modern"


@pytest.mark.parametrize(
    ("pack_format", "version"),
    [(97.1, [97, 1]), (121.0, [121, 0]), (88.0, [88, 0]), (107.1, [107, 1])],
)
def test_pack_metadata_adds_required_format_range(
    pack_format: float, version: list[int],
) -> None:
    current = pack_metadata(pack_format, "current")
    legacy = pack_metadata(64.0, "legacy")

    assert current["min_format"] == version
    assert current["max_format"] == version
    assert "min_format" not in legacy
    assert "max_format" not in legacy


def test_previous_version_pack_formats_can_be_selected_explicitly() -> None:
    parser = build_parser()
    bank_args = parser.parse_args(["bank-build", "--pack-format", "88.0"])
    set_args = parser.parse_args(["bank-build-set", "--pack-format", "88.0"])
    convert_args = parser.parse_args([
        "convert", "input.wav", "--data-pack-format", "107.1",
    ])

    assert bank_args.pack_format == set_args.pack_format == 88.0
    assert convert_args.data_pack_format == 107.1
