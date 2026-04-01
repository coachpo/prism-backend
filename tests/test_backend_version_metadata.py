from __future__ import annotations

import importlib
from importlib import metadata as importlib_metadata
from pathlib import Path
import sys
from typing import Any, cast

from httpx import ASGITransport, AsyncClient


pytest = cast(Any, importlib.import_module("pytest"))


def _reload_main_module(
    monkeypatch: Any,
    *,
    installed_version: str | None = None,
    backend_version: str | None = None,
):
    if installed_version is None:

        def raise_package_not_found(_: str) -> str:
            raise importlib_metadata.PackageNotFoundError("prism-backend")

        monkeypatch.setattr(importlib_metadata, "version", raise_package_not_found)
    else:
        monkeypatch.setattr(importlib_metadata, "version", lambda _: installed_version)

    if backend_version is not None:
        original_read_text = Path.read_text

        def fake_read_text(self: Path, *args, **kwargs) -> str:
            if self.name == "VERSION" and self.parent.name == "backend":
                return f"{backend_version}\n"
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fake_read_text)

    sys.modules.pop("app.core.version", None)
    sys.modules.pop("app.main", None)
    return importlib.import_module("app.main")


def test_fastapi_metadata_version_uses_backend_version_source(
    monkeypatch: Any,
):
    main_module = _reload_main_module(monkeypatch, installed_version="9.8.7")

    assert main_module.app.version == "9.8.7"


@pytest.mark.asyncio
async def test_health_reports_same_backend_version_when_package_metadata_is_unavailable(
    monkeypatch: Any,
):
    main_module = _reload_main_module(
        monkeypatch,
        installed_version=None,
        backend_version="7.6.5",
    )

    async with AsyncClient(
        transport=ASGITransport(app=main_module.app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "7.6.5"}
    assert main_module.app.version == "7.6.5"
