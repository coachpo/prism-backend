from __future__ import annotations

from functools import lru_cache
from importlib import metadata as importlib_metadata
from pathlib import Path


BACKEND_DISTRIBUTION_NAME = "prism-backend"
BACKEND_VERSION_PATH = Path(__file__).resolve().parents[2] / "VERSION"


def _read_backend_local_version() -> str:
    try:
        version = BACKEND_VERSION_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Backend version source not found at {BACKEND_VERSION_PATH}"
        ) from exc

    if not version:
        raise RuntimeError(f"Backend version source is empty: {BACKEND_VERSION_PATH}")

    return version


@lru_cache(maxsize=1)
def get_backend_version() -> str:
    try:
        return importlib_metadata.version(BACKEND_DISTRIBUTION_NAME)
    except importlib_metadata.PackageNotFoundError:
        return _read_backend_local_version()
