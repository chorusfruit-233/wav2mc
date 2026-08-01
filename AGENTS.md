# Repository Guidelines

## Project Structure & Module Organization

The installable package uses a `src` layout. Production code lives in `src/wav2mc/`: `audio.py` handles decoding, `analysis.py` extracts components, `preview.py` reconstructs audio, and `loudness.py` calibrates Minecraft volume. `bank.py`/`datapack.py` generate packs, `pipeline.py` coordinates conversion, and `cli.py` defines the command. Keep shared defaults and data classes in `config.py`.

User guides belong in `docs/`; tests belong in `tests/`; reusable examples belong in `examples/`. `demo/` contains reference inputs and artifacts. Do not commit routine output, virtual environments, caches, or generated audio/ZIP files unless they are intentional demo fixtures.

## Build, Test, and Development Commands

Use Python 3.10+ and ensure `ffmpeg` is available on `PATH`.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m pip install pytest
pytest -q
wav2mc --help
```

The editable install exposes local source changes immediately. To exercise the main workflows:

```bash
wav2mc bank-build --output output/test_bank.zip --max-frequency 1000
wav2mc convert demo/test_tone.wav --name test_tone --max-frequency 1000
```

## Coding Style & Naming Conventions

Follow standard Python conventions: four-space indentation, `snake_case` for functions and modules, `PascalCase` for classes, and uppercase names for constants. Add type hints to public functions and keep `from __future__ import annotations` in modules that use deferred annotations. Prefer `pathlib.Path`, frozen dataclasses for value objects, and small functions with explicit validation. No formatter or linter is currently configured; keep changes consistent with the existing PEP 8-style code and avoid unrelated formatting churn.

## Testing Guidelines

Tests use pytest with function names beginning `test_`. Add focused tests to `tests/test_*.py`, use `tmp_path` for generated files, and avoid writing into the repository. Cover numerical boundary behavior, invalid configuration, and ZIP contents when changing analysis or pack generation. There is no formal coverage threshold; every bug fix should include a regression test. Run `pytest -q` before submitting.

## Commit & Pull Request Guidelines

Git history is not included in this checkout, so no existing message convention can be inferred. Use short, imperative subjects, optionally with Conventional Commit prefixes such as `fix: reject invalid frequency ranges`. Keep commits scoped to one behavior.

Pull requests should explain the user-visible change, list verification commands, and link relevant issues. Include small output excerpts or artifact details for conversion changes; attach screenshots only when Minecraft-visible behavior changes. Note any change to pack formats, audio defaults, or FFmpeg assumptions explicitly.
