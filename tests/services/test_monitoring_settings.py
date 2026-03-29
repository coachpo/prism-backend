import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.routing import APIRoute


def _load_module(module_name: str):
    try:
        return importlib.import_module(module_name)
    except Exception as exc:
        pytest.fail(f"{module_name} must exist for monitoring settings: {exc}")


def _require_attr(module: object, attr_name: str):
    value = getattr(module, attr_name, None)
    module_name = getattr(module, "__name__", type(module).__name__)
    assert value is not None, f"{module_name}.{attr_name} must exist"
    return value


class TestMonitoringSettingsContract:
    def test_settings_router_mounts_monitoring_routes(self):
        settings_module = _load_module("app.routers.settings")

        registered_routes = {
            (route.path, method)
            for route in settings_module.router.routes
            if isinstance(route, APIRoute)
            for method in route.methods or set()
        }

        assert ("/api/settings/monitoring", "GET") in registered_routes
        assert ("/api/settings/monitoring", "PUT") in registered_routes

    def test_monitoring_settings_schemas_are_exported(self):
        schemas = _load_module("app.schemas.schemas")

        _require_attr(schemas, "MonitoringSettingsResponse")
        _require_attr(schemas, "MonitoringSettingsUpdate")

    @pytest.mark.asyncio
    async def test_get_monitoring_settings_route_returns_profile_scoped_cadence_payload(
        self,
    ):
        handlers = _load_module(
            "app.routers.settings_domains.monitoring_route_handlers"
        )
        get_monitoring_settings = _require_attr(handlers, "get_monitoring_settings")

        mock_db = AsyncMock()
        settings_row = MagicMock(monitoring_probe_interval_seconds=240)

        with patch(
            "app.routers.settings_domains.monitoring_route_handlers.get_or_create_user_settings",
            new_callable=AsyncMock,
        ) as mock_get_or_create_user_settings:
            mock_get_or_create_user_settings.return_value = settings_row
            response = await get_monitoring_settings(db=mock_db, profile_id=7)

        assert response.profile_id == 7
        assert response.monitoring_probe_interval_seconds == 240

    @pytest.mark.asyncio
    async def test_update_monitoring_settings_route_clamps_interval_to_backend_bounds(
        self,
    ):
        handlers = _load_module(
            "app.routers.settings_domains.monitoring_route_handlers"
        )
        update_monitoring_settings = _require_attr(
            handlers, "update_monitoring_settings"
        )
        minimum = _require_attr(handlers, "MIN_MONITORING_PROBE_INTERVAL_SECONDS")
        maximum = _require_attr(handlers, "MAX_MONITORING_PROBE_INTERVAL_SECONDS")

        mock_db = AsyncMock()
        mock_db.flush = AsyncMock()
        settings_row = MagicMock(monitoring_probe_interval_seconds=None)

        with patch(
            "app.routers.settings_domains.monitoring_route_handlers.get_or_create_user_settings",
            new_callable=AsyncMock,
        ) as mock_get_or_create_user_settings:
            mock_get_or_create_user_settings.return_value = settings_row

            below_minimum = await update_monitoring_settings(
                body=SimpleNamespace(monitoring_probe_interval_seconds=minimum - 1),
                db=mock_db,
                profile_id=7,
            )
            assert settings_row.monitoring_probe_interval_seconds == minimum
            assert below_minimum.monitoring_probe_interval_seconds == minimum

            above_maximum = await update_monitoring_settings(
                body=SimpleNamespace(monitoring_probe_interval_seconds=maximum + 1),
                db=mock_db,
                profile_id=7,
            )
            assert settings_row.monitoring_probe_interval_seconds == maximum
            assert above_maximum.monitoring_probe_interval_seconds == maximum

            in_range_value = minimum + 15
            exact_value = await update_monitoring_settings(
                body=SimpleNamespace(monitoring_probe_interval_seconds=in_range_value),
                db=mock_db,
                profile_id=7,
            )
            assert settings_row.monitoring_probe_interval_seconds == in_range_value
            assert exact_value.monitoring_probe_interval_seconds == in_range_value

        assert mock_db.flush.await_count == 3
