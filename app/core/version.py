from __future__ import annotations

from functools import lru_cache
from importlib import metadata as importlib_metadata
from pathlib import Path
import tomllib
from typing import cast


BACKEND_DISTRIBUTION_NAME = "prism-backend"
BACKEND_PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _read_backend_pyproject_version() -> str:
    try:
        pyproject_data = cast(
            dict[str, object],
            tomllib.loads(BACKEND_PYPROJECT_PATH.read_text(encoding="utf-8")),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Backend version source not found at {BACKEND_PYPROJECT_PATH}"
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(
            f"Backend version source is invalid TOML: {BACKEND_PYPROJECT_PATH}"
        ) from exc

    project_data = pyproject_data.get("project")
    if not isinstance(project_data, dict):
        raise RuntimeError(
            f"Backend version source is missing [project]: {BACKEND_PYPROJECT_PATH}"
        )

    typed_project_data = cast(dict[str, object], project_data)
    version = typed_project_data.get("version")
    if not isinstance(version, str) or not version:
        raise RuntimeError(
            f"Backend version source is missing project.version: {BACKEND_PYPROJECT_PATH}"
        )

    return version


@lru_cache(maxsize=1)
def get_backend_version() -> str:
    try:
        return importlib_metadata.version(BACKEND_DISTRIBUTION_NAME)
    except importlib_metadata.PackageNotFoundError:
        return _read_backend_pyproject_version()
