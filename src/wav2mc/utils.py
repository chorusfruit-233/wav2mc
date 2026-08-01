from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_NAMESPACE_RE = re.compile(r"[^a-z0-9_.-]+")


def safe_namespace(value: str) -> str:
    cleaned = _NAMESPACE_RE.sub("_", value.lower().strip())
    cleaned = cleaned.strip("._-")
    return cleaned or "song"


def ensure_command(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(
            f"Required command '{name}' was not found in PATH. "
            "Install FFmpeg and try again."
        )
    return path


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def pack_metadata(pack_format: float, description: str) -> dict[str, object]:
    metadata: dict[str, object] = {
        "pack_format": pack_format,
        "description": description,
    }
    if pack_format > 64:
        major_text, separator, minor_text = str(pack_format).partition(".")
        version = [
            int(major_text),
            int(minor_text.rstrip("0") or "0") if separator else 0,
        ]
        metadata["min_format"] = version
        metadata["max_format"] = version
    return metadata


def zip_directory(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(source.rglob("*")):
            if item.is_file():
                archive.write(item, item.relative_to(source).as_posix())


@contextmanager
def temporary_directory(prefix: str = "wav2mc-") -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix=prefix) as raw:
        yield Path(raw)
