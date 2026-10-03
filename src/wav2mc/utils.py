from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


_NAMESPACE_RE = re.compile(r"[^a-z0-9_.-]+")


@dataclass(frozen=True)
class ProgressUpdate:
    stage: str
    fraction: float
    detail: str = ""


ProgressCallback = Callable[[ProgressUpdate], None]
CancelCheck = Callable[[], bool]


class TaskCancelled(RuntimeError):
    """Raised when a cooperative long-running task is cancelled."""


def check_cancelled(cancel_check: CancelCheck | None) -> None:
    if cancel_check is not None and cancel_check():
        raise TaskCancelled("Task cancelled")


def emit_progress(
    callback: ProgressCallback | None,
    stage: str,
    fraction: float,
    detail: str = "",
) -> None:
    if callback is not None:
        callback(ProgressUpdate(stage, min(1.0, max(0.0, fraction)), detail))


def scaled_progress(
    callback: ProgressCallback | None,
    start: float,
    end: float,
    stage: str | None = None,
) -> ProgressCallback | None:
    if callback is None:
        return None

    def forward(update: ProgressUpdate) -> None:
        callback(
            ProgressUpdate(
                stage or update.stage,
                start + (end - start) * update.fraction,
                update.detail,
            )
        )

    return forward


def safe_namespace(value: str) -> str:
    cleaned = _NAMESPACE_RE.sub("_", value.lower().strip())
    cleaned = cleaned.strip("._-")
    return cleaned or "song"


def song_namespace(value: str) -> str:
    """Keep Unicode song names distinct using Minecraft-safe identifiers."""
    namespace = safe_namespace(value)
    if not value.isascii():
        digest = hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:12]
        return f"{namespace}_{digest}"
    return namespace


def safe_output_name(value: str) -> str:
    """Preserve Unicode while removing unsafe filename characters."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", value.strip())
    cleaned = cleaned.strip(" .") or "song"
    if cleaned.split(".")[0].upper() in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        cleaned = f"_{cleaned}"
    # Leave space for suffixes on filesystems with a 255-byte name limit.
    return cleaned.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" .")


def conversion_output_paths(output_dir: Path, song_name: str) -> dict[str, Path]:
    name = safe_output_name(song_name)
    return {
        "data_pack": output_dir / f"{name}_datapack.zip",
        "preview": output_dir / f"{name}_preview.wav",
        "report": output_dir / f"{name}_analysis.json",
    }


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


def zip_directory(
    source: Path,
    target: Path,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    files = [item for item in sorted(source.rglob("*")) if item.is_file()]
    with tempfile.NamedTemporaryFile(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
        delete=False,
    ) as temporary:
        staged_target = Path(temporary.name)
    try:
        check_cancelled(cancel_check)
        with zipfile.ZipFile(
            staged_target,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for index, item in enumerate(files):
                check_cancelled(cancel_check)
                archive.write(item, item.relative_to(source).as_posix())
                if index % 32 == 0 or index + 1 == len(files):
                    emit_progress(
                        progress_callback,
                        "compress",
                        (index + 1) / max(1, len(files)),
                        f"{index + 1}/{len(files)} files",
                    )
        check_cancelled(cancel_check)
        staged_target.replace(target)
        emit_progress(progress_callback, "compress", 1.0, "Archive complete")
    except Exception:
        staged_target.unlink(missing_ok=True)
        raise


@contextmanager
def temporary_directory(
    prefix: str = "wav2mc-",
    directory: Path | None = None,
) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(
        prefix=prefix,
        dir=str(directory) if directory is not None else None,
    ) as raw:
        yield Path(raw)
