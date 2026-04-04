import inspect
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException
from fastapi.routing import APIRoute


def _request_log_row(**overrides):
    payload = {
        "id": 321,
        "profile_id": 7,
        "model_id": "gpt-5.4",
        "api_family": "openai",
        "vendor_id": 1,
        "vendor_key": "openai",
        "vendor_name": "OpenAI",
        "resolved_target_model_id": "gpt-4.1-mini",
        "endpoint_id": 4,
        "connection_id": 8,
        "proxy_api_key_id": 12,
        "proxy_api_key_name_snapshot": "primary-key",
        "ingress_request_id": "ingress-321",
        "attempt_number": 2,
        "provider_correlation_id": "provider-321",
        "endpoint_base_url": "https://api.openai.com",
        "status_code": 503,
        "response_time_ms": 1450,
        "is_stream": False,
        "input_tokens": 120,
        "output_tokens": 80,
        "total_tokens": 200,
        "success_flag": False,
        "billable_flag": False,
        "priced_flag": False,
        "unpriced_reason": "missing-price",
        "cache_read_input_tokens": 10,
        "cache_creation_input_tokens": 20,
        "reasoning_tokens": 30,
        "input_cost_micros": 100,
        "output_cost_micros": 200,
        "cache_read_input_cost_micros": 30,
        "cache_creation_input_cost_micros": 40,
        "reasoning_cost_micros": 50,
        "total_cost_original_micros": 600,
        "total_cost_user_currency_micros": 700,
        "currency_code_original": "USD",
        "report_currency_code": "USD",
        "report_currency_symbol": "$",
        "fx_rate_used": "1",
        "fx_rate_source": "manual",
        "pricing_snapshot_unit": "tokens",
        "pricing_snapshot_input": "0.1",
        "pricing_snapshot_output": "0.2",
        "pricing_snapshot_cache_read_input": "0.01",
        "pricing_snapshot_cache_creation_input": "0.02",
        "pricing_snapshot_reasoning": "0.03",
        "pricing_snapshot_missing_special_token_price_policy": "zero",
        "pricing_config_version_used": 4,
        "request_path": "/v1/chat/completions",
        "error_detail": "upstream timeout",
        "endpoint_description": "Primary OpenAI endpoint",
        "created_at": datetime(2026, 2, 28, 3, 29, 6, 216000, tzinfo=timezone.utc),
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


class TestDEF004_FrontendDeleteErrorHandling:
    """DEF-004 (P2): frontend must show error toast on failed model delete."""

    def test_api_client_throws_error_with_detail_message(self):
        pass


class TestDEF058_StatsTimezoneFilterNormalization:
    """DEF-058 (P1): stats endpoints must accept ISO-8601 `Z` datetime filters."""

    @staticmethod
    def _aware_utc_datetime():
        from datetime import datetime, timezone

        return datetime(2026, 2, 28, 3, 29, 6, 216000, tzinfo=timezone.utc)

    @pytest.mark.asyncio
    async def test_requests_route_normalizes_aware_datetimes_before_service_call(self):
        from app.routers.stats import list_request_logs

        mock_db = AsyncMock()
        aware_from = self._aware_utc_datetime()

        with patch(
            "app.routers.stats.get_request_logs", new_callable=AsyncMock
        ) as mock_get_request_logs:
            mock_get_request_logs.return_value = ([], 0)
            response = await list_request_logs(
                db=mock_db,
                profile_id=7,
                from_time=aware_from,
                limit=50,
                offset=0,
            )

        assert response.total == 0
        _, call_kwargs = cast(
            tuple[tuple[object, ...], dict[str, object]],
            mock_get_request_logs.await_args_list[0],
        )
        from_time: datetime = cast(datetime, call_kwargs["from_time"])
        assert from_time == aware_from
        assert from_time.tzinfo is not None
        assert "to_time" not in call_kwargs

    def test_requests_route_signature_keeps_only_simplified_browse_query_params(self):
        from app.routers.stats import list_request_logs

        parameter_names = set(inspect.signature(list_request_logs).parameters)

        assert {
            "db",
            "profile_id",
            "ingress_request_id",
            "model_id",
            "status_family",
            "from_time",
            "endpoint_id",
            "limit",
            "offset",
        }.issubset(parameter_names)
        assert "request_id" not in parameter_names
        assert "api_family" not in parameter_names
        assert "status_code" not in parameter_names
        assert "success" not in parameter_names
        assert "to_time" not in parameter_names
        assert "connection_id" not in parameter_names

    @pytest.mark.asyncio
    async def test_requests_route_passes_status_family_filter_to_service(self):
        from app.routers.stats import list_request_logs

        mock_db = AsyncMock()

        with patch(
            "app.routers.stats.get_request_logs", new_callable=AsyncMock
        ) as mock_get_request_logs:
            mock_get_request_logs.return_value = ([], 0)
            response = await list_request_logs(
                db=mock_db,
                profile_id=7,
                status_family="4xx",
                limit=50,
                offset=0,
            )

        assert response.total == 0
        _, call_kwargs = cast(
            tuple[tuple[object, ...], dict[str, object]],
            mock_get_request_logs.await_args_list[0],
        )
        assert call_kwargs["status_family"] == "4xx"
        assert set(call_kwargs) == {
            "ingress_request_id",
            "model_id",
            "profile_id",
            "status_family",
            "from_time",
            "endpoint_id",
            "limit",
            "offset",
        }

    @pytest.mark.asyncio
    async def test_requests_route_serializes_slim_list_items(self):
        from app.routers.stats import list_request_logs

        mock_db = AsyncMock()

        with patch(
            "app.routers.stats.get_request_logs", new_callable=AsyncMock
        ) as mock_get_request_logs:
            mock_get_request_logs.return_value = ([_request_log_row()], 1)
            response = await list_request_logs(
                db=mock_db,
                profile_id=7,
                limit=50,
                offset=0,
            )

        assert response.total == 1
        assert response.items[0].vendor_name == "OpenAI"
        item_payload = response.items[0].model_dump()
        assert "endpoint_base_url" not in item_payload
        assert "pricing_snapshot_input" not in item_payload
        assert "proxy_api_key_name_snapshot" not in item_payload

    @pytest.mark.asyncio
    async def test_request_detail_route_passes_profile_scoped_request_id_to_service(
        self,
    ):
        from app.routers.stats import request_log_detail

        mock_db = AsyncMock()

        with patch(
            "app.routers.stats.get_request_log_detail", new_callable=AsyncMock
        ) as mock_get_request_log_detail:
            mock_get_request_log_detail.return_value = _request_log_row()
            response = await request_log_detail(
                db=mock_db,
                profile_id=7,
                request_id=321,
            )

        _, call_kwargs = cast(
            tuple[tuple[object, ...], dict[str, object]],
            mock_get_request_log_detail.await_args_list[0],
        )
        assert call_kwargs["profile_id"] == 7
        assert call_kwargs["request_id"] == 321
        assert response.summary.id == 321
        assert response.request.ingress_request_id == "ingress-321"
        assert response.routing.vendor_name == "OpenAI"
        assert response.usage.total_tokens == 200
        assert response.costing.report_currency_symbol == "$"
        assert response.pricing.pricing_snapshot_input == "0.1"

    @pytest.mark.asyncio
    async def test_request_detail_route_returns_404_for_missing_or_out_of_scope_request(
        self,
    ):
        from app.routers.stats import request_log_detail

        mock_db = AsyncMock()

        with patch(
            "app.routers.stats.get_request_log_detail", new_callable=AsyncMock
        ) as mock_get_request_log_detail:
            mock_get_request_log_detail.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await request_log_detail(
                    db=mock_db,
                    profile_id=7,
                    request_id=404,
                )

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "Request log not found"

    def test_operations_requests_route_is_absent(self):
        import app.routers.stats as stats_router

        registered_paths = {
            route.path
            for route in stats_router.router.routes
            if isinstance(route, APIRoute)
        }
        removed_route_path = "/api/stats/requests/" + "operations"
        removed_handler_name = "list_" + "operations_request_logs"
        removed_connection_metrics_path = "/api/stats/models/connections/" + "metrics"
        removed_connection_metrics_handler_name = "connection_" + "metrics_batch"

        assert removed_route_path not in registered_paths
        assert "/api/stats/requests/{request_id}" in registered_paths
        assert not hasattr(stats_router, removed_handler_name)
        assert removed_connection_metrics_path not in registered_paths
        assert not hasattr(stats_router, removed_connection_metrics_handler_name)

    @pytest.mark.asyncio
    async def test_summary_route_normalizes_aware_datetimes_before_service_call(self):
        from app.routers.stats import stats_summary

        mock_db = AsyncMock()
        aware_from = self._aware_utc_datetime()
        aware_to = self._aware_utc_datetime()

        with patch(
            "app.routers.stats.get_stats_summary", new_callable=AsyncMock
        ) as mock_get_stats_summary:
            mock_get_stats_summary.return_value = {
                "total_requests": 0,
                "success_count": 0,
                "error_count": 0,
                "success_rate": 0.0,
                "avg_response_time_ms": 0.0,
                "p95_response_time_ms": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_tokens": 0,
                "groups": [],
            }
            response = await stats_summary(
                db=mock_db,
                profile_id=7,
                from_time=aware_from,
                to_time=aware_to,
            )

        assert response.total_requests == 0
        _, call_kwargs = cast(
            tuple[tuple[object, ...], dict[str, object]],
            mock_get_stats_summary.await_args_list[0],
        )
        from_time: datetime = cast(datetime, call_kwargs["from_time"])
        to_time: datetime = cast(datetime, call_kwargs["to_time"])
        assert from_time == aware_from
        assert to_time == aware_to
        assert from_time.tzinfo is not None
        assert to_time.tzinfo is not None

    @pytest.mark.asyncio
    async def test_connection_success_rates_route_normalizes_aware_datetimes(self):
        from app.routers.stats import connection_success_rates

        mock_db = AsyncMock()
        aware_from = self._aware_utc_datetime()
        aware_to = self._aware_utc_datetime()

        with patch(
            "app.routers.stats.get_connection_success_rates", new_callable=AsyncMock
        ) as mock_get_success_rates:
            mock_get_success_rates.return_value = []
            response = await connection_success_rates(
                db=mock_db,
                profile_id=7,
                from_time=aware_from,
                to_time=aware_to,
            )

        assert response == []
        _, call_kwargs = cast(
            tuple[tuple[object, ...], dict[str, object]],
            mock_get_success_rates.await_args_list[0],
        )
        from_time: datetime = cast(datetime, call_kwargs["from_time"])
        to_time: datetime = cast(datetime, call_kwargs["to_time"])
        assert from_time == aware_from
        assert to_time == aware_to
        assert from_time.tzinfo is not None
        assert to_time.tzinfo is not None

    @pytest.mark.asyncio
    async def test_spending_route_normalizes_aware_datetimes_before_service_call(self):
        from app.routers.stats import spending_report

        mock_db = AsyncMock()
        aware_from = self._aware_utc_datetime()
        aware_to = self._aware_utc_datetime()

        with patch(
            "app.routers.stats.get_spending_report", new_callable=AsyncMock
        ) as mock_get_spending_report:
            mock_get_spending_report.return_value = {
                "summary": {
                    "total_cost_micros": 0,
                    "successful_request_count": 0,
                    "priced_request_count": 0,
                    "unpriced_request_count": 0,
                    "total_input_tokens": 0,
                    "total_output_tokens": 0,
                    "total_cache_read_input_tokens": 0,
                    "total_cache_creation_input_tokens": 0,
                    "total_reasoning_tokens": 0,
                    "total_tokens": 0,
                    "avg_cost_per_successful_request_micros": 0,
                },
                "groups": [],
                "groups_total": 0,
                "top_spending_models": [],
                "top_spending_endpoints": [],
                "unpriced_breakdown": {},
                "report_currency_code": "USD",
                "report_currency_symbol": "$",
            }
            response = await spending_report(
                db=mock_db,
                profile_id=7,
                from_time=aware_from,
                to_time=aware_to,
            )

        assert response["report_currency_code"] == "USD"
        _, call_kwargs = cast(
            tuple[tuple[object, ...], dict[str, object]],
            mock_get_spending_report.await_args_list[0],
        )
        from_time: datetime = cast(datetime, call_kwargs["from_time"])
        to_time: datetime = cast(datetime, call_kwargs["to_time"])
        assert from_time == aware_from
        assert to_time == aware_to
        assert from_time.tzinfo is not None
        assert to_time.tzinfo is not None

    @pytest.mark.asyncio
    async def test_model_metrics_batch_route_passes_batch_filters_to_service(self):
        from app.routers.stats import model_metrics_batch
        from app.schemas.schemas import ModelMetricsBatchRequest

        mock_db = AsyncMock()

        with patch(
            "app.routers.stats.get_model_metrics_batch", new_callable=AsyncMock
        ) as mock_get_model_metrics_batch:
            mock_get_model_metrics_batch.return_value = {
                "gpt-5.4": {
                    "success_rate": 99.5,
                    "request_count_24h": 12,
                    "p95_latency_ms": 880,
                    "spend_30d_micros": 123456,
                }
            }
            response = await model_metrics_batch(
                body=ModelMetricsBatchRequest(
                    model_ids=["gpt-5.4"],
                    summary_window_hours=24,
                    spending_preset="last_30_days",
                ),
                db=mock_db,
                profile_id=7,
            )

        assert len(response.items) == 1
        assert response.items[0].model_id == "gpt-5.4"
        _, call_kwargs = cast(
            tuple[tuple[object, ...], dict[str, object]],
            mock_get_model_metrics_batch.await_args_list[0],
        )
        assert call_kwargs["profile_id"] == 7
        assert call_kwargs["model_ids"] == ["gpt-5.4"]
        assert call_kwargs["summary_window_hours"] == 24
        assert call_kwargs["spending_preset"] == "last_30_days"

    @pytest.mark.asyncio
    async def test_get_timezone_preference_route_returns_lightweight_payload(self):
        from app.routers.settings_domains.costing_route_handlers import (
            get_timezone_preference,
        )

        mock_db = AsyncMock()
        settings_row = MagicMock(timezone_preference="Europe/Helsinki")

        with patch(
            "app.routers.settings_domains.costing_route_handlers.get_or_create_user_settings",
            new_callable=AsyncMock,
        ) as mock_get_or_create_user_settings:
            mock_get_or_create_user_settings.return_value = settings_row
            response = await get_timezone_preference(db=mock_db, profile_id=7)

        assert response.profile_id == 7
        assert response.timezone_preference == "Europe/Helsinki"

    @pytest.mark.asyncio
    async def test_update_timezone_preference_route_persists_value(self):
        from app.routers.settings_domains.costing_route_handlers import (
            update_timezone_preference,
        )
        from app.schemas.schemas import TimezonePreferenceUpdate

        mock_db = AsyncMock()
        mock_db.flush = AsyncMock()
        settings_row = MagicMock(timezone_preference=None)

        with patch(
            "app.routers.settings_domains.costing_route_handlers.get_or_create_user_settings",
            new_callable=AsyncMock,
        ) as mock_get_or_create_user_settings:
            mock_get_or_create_user_settings.return_value = settings_row
            response = await update_timezone_preference(
                body=TimezonePreferenceUpdate(timezone_preference="UTC"),
                db=mock_db,
                profile_id=7,
            )

        assert settings_row.timezone_preference == "UTC"
        assert response.profile_id == 7
        assert response.timezone_preference == "UTC"
        mock_db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_model_connections_batch_route_passes_model_ids_to_service(self):
        from app.routers.connections import list_connections_batch
        from app.schemas.schemas import ModelConnectionsBatchRequest

        mock_db = AsyncMock()

        with patch(
            "app.routers.connections.list_connections_for_models",
            new_callable=AsyncMock,
        ) as mock_list_connections_for_models:
            mock_list_connections_for_models.return_value = {10: []}
            response = await list_connections_batch(
                body=ModelConnectionsBatchRequest(model_config_ids=[10]),
                db=mock_db,
                profile_id=7,
            )

        assert len(response.items) == 1
        assert response.items[0].model_config_id == 10
        _, call_kwargs = cast(
            tuple[tuple[object, ...], dict[str, object]],
            mock_list_connections_for_models.await_args_list[0],
        )
        assert call_kwargs["profile_id"] == 7
        assert call_kwargs["model_config_ids"] == [10]


class TestBatchDeleteValidation:
    """Validate flexible batch deletion for stats and audit endpoints."""

    @pytest.mark.asyncio
    async def test_stats_delete_custom_days(self):
        """Stats delete accepts any integer >= 1 for older_than_days."""
        from app.routers.stats import delete_request_logs

        background_tasks = BackgroundTasks()

        with patch(
            "app.routers.stats.delete_request_logs_in_background",
            new_callable=AsyncMock,
        ) as mock_delete_request_logs:
            response = await delete_request_logs(
                background_tasks=background_tasks,
                profile_id=1,
                older_than_days=45,
                delete_all=False,
            )
            await background_tasks()

        assert response.accepted is True
        mock_delete_request_logs.assert_awaited_once_with(
            profile_id=1,
            older_than_days=45,
            delete_all=False,
        )

    @pytest.mark.asyncio
    async def test_stats_delete_all(self):
        """Stats delete_all=true deletes all request logs."""
        from app.routers.stats import delete_request_logs

        background_tasks = BackgroundTasks()

        with patch(
            "app.routers.stats.delete_request_logs_in_background",
            new_callable=AsyncMock,
        ) as mock_delete_request_logs:
            response = await delete_request_logs(
                background_tasks=background_tasks,
                profile_id=1,
                older_than_days=None,
                delete_all=True,
            )
            await background_tasks()

        assert response.accepted is True
        mock_delete_request_logs.assert_awaited_once_with(
            profile_id=1,
            older_than_days=None,
            delete_all=True,
        )

    @pytest.mark.asyncio
    async def test_stats_delete_rejects_both_modes(self):
        """Stats delete rejects older_than_days + delete_all=true."""
        from app.routers.stats import delete_request_logs

        background_tasks = BackgroundTasks()
        with pytest.raises(HTTPException) as exc_info:
            await delete_request_logs(
                background_tasks=background_tasks,
                profile_id=1,
                older_than_days=7,
                delete_all=True,
            )
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_stats_delete_rejects_neither_mode(self):
        """Stats delete rejects when neither mode is provided."""
        from app.routers.stats import delete_request_logs

        background_tasks = BackgroundTasks()
        with pytest.raises(HTTPException) as exc_info:
            await delete_request_logs(
                background_tasks=background_tasks,
                profile_id=1,
                older_than_days=None,
                delete_all=False,
            )
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_statistics_delete_custom_days(self):
        """Statistics delete accepts any integer >= 1 for older_than_days."""
        from app.routers.stats import delete_statistics_data

        background_tasks = BackgroundTasks()

        with patch(
            "app.routers.stats.delete_statistics_in_background",
            new_callable=AsyncMock,
        ) as mock_delete_statistics:
            response = await delete_statistics_data(
                background_tasks=background_tasks,
                profile_id=1,
                older_than_days=45,
                delete_all=False,
            )
            await background_tasks()

        assert response.accepted is True
        mock_delete_statistics.assert_awaited_once_with(
            profile_id=1,
            older_than_days=45,
            delete_all=False,
        )

    @pytest.mark.asyncio
    async def test_statistics_delete_all(self):
        """Statistics delete_all=true deletes all persisted statistics rows."""
        from app.routers.stats import delete_statistics_data

        background_tasks = BackgroundTasks()

        with patch(
            "app.routers.stats.delete_statistics_in_background",
            new_callable=AsyncMock,
        ) as mock_delete_statistics:
            response = await delete_statistics_data(
                background_tasks=background_tasks,
                profile_id=1,
                older_than_days=None,
                delete_all=True,
            )
            await background_tasks()

        assert response.accepted is True
        mock_delete_statistics.assert_awaited_once_with(
            profile_id=1,
            older_than_days=None,
            delete_all=True,
        )

    @pytest.mark.asyncio
    async def test_statistics_delete_rejects_both_modes(self):
        """Statistics delete rejects older_than_days + delete_all=true."""
        from app.routers.stats import delete_statistics_data

        background_tasks = BackgroundTasks()
        with pytest.raises(HTTPException) as exc_info:
            await delete_statistics_data(
                background_tasks=background_tasks,
                profile_id=1,
                older_than_days=7,
                delete_all=True,
            )
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_statistics_delete_rejects_neither_mode(self):
        """Statistics delete rejects when neither mode is provided."""
        from app.routers.stats import delete_statistics_data

        background_tasks = BackgroundTasks()
        with pytest.raises(HTTPException) as exc_info:
            await delete_statistics_data(
                background_tasks=background_tasks,
                profile_id=1,
                older_than_days=None,
                delete_all=False,
            )
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_audit_delete_custom_days(self):
        """Audit delete accepts any integer >= 1 for older_than_days."""
        from app.routers.audit import delete_audit_logs

        background_tasks = BackgroundTasks()

        with patch(
            "app.routers.audit.delete_audit_logs_in_background",
            new_callable=AsyncMock,
        ) as mock_delete_audit_logs:
            response = await delete_audit_logs(
                background_tasks=background_tasks,
                profile_id=1,
                before=None,
                older_than_days=45,
                delete_all=False,
            )
            await background_tasks()

        assert response.accepted is True
        mock_delete_audit_logs.assert_awaited_once_with(
            profile_id=1,
            before=None,
            older_than_days=45,
            delete_all=False,
        )

    @pytest.mark.asyncio
    async def test_audit_delete_all(self):
        """Audit delete_all=true deletes all audit logs."""
        from app.routers.audit import delete_audit_logs

        background_tasks = BackgroundTasks()

        with patch(
            "app.routers.audit.delete_audit_logs_in_background",
            new_callable=AsyncMock,
        ) as mock_delete_audit_logs:
            response = await delete_audit_logs(
                background_tasks=background_tasks,
                profile_id=1,
                before=None,
                older_than_days=None,
                delete_all=True,
            )
            await background_tasks()

        assert response.accepted is True
        mock_delete_audit_logs.assert_awaited_once_with(
            profile_id=1,
            before=None,
            older_than_days=None,
            delete_all=True,
        )

    @pytest.mark.asyncio
    async def test_audit_delete_before_still_works(self):
        """Audit delete with 'before' datetime still works."""
        from datetime import datetime
        from app.routers.audit import delete_audit_logs

        cutoff = datetime(2025, 1, 1)
        background_tasks = BackgroundTasks()

        with patch(
            "app.routers.audit.delete_audit_logs_in_background",
            new_callable=AsyncMock,
        ) as mock_delete_audit_logs:
            response = await delete_audit_logs(
                background_tasks=background_tasks,
                profile_id=1,
                before=cutoff,
                older_than_days=None,
                delete_all=False,
            )
            await background_tasks()

        assert response.accepted is True
        _, call_kwargs = cast(
            tuple[tuple[object, ...], dict[str, object]],
            mock_delete_audit_logs.await_args_list[0],
        )
        normalized_before = cast(datetime, call_kwargs["before"])
        assert normalized_before.tzinfo is not None

    @pytest.mark.asyncio
    async def test_audit_delete_rejects_multiple_modes(self):
        """Audit delete rejects when multiple modes are provided."""
        from datetime import datetime
        from app.routers.audit import delete_audit_logs

        background_tasks = BackgroundTasks()

        # before + older_than_days
        with pytest.raises(HTTPException) as exc_info:
            await delete_audit_logs(
                background_tasks=background_tasks,
                profile_id=1,
                before=datetime(2025, 1, 1),
                older_than_days=7,
                delete_all=False,
            )
        assert exc_info.value.status_code == 400

        # older_than_days + delete_all
        with pytest.raises(HTTPException) as exc_info:
            await delete_audit_logs(
                background_tasks=background_tasks,
                profile_id=1,
                before=None,
                older_than_days=7,
                delete_all=True,
            )
        assert exc_info.value.status_code == 400

        # all three
        with pytest.raises(HTTPException) as exc_info:
            await delete_audit_logs(
                background_tasks=background_tasks,
                profile_id=1,
                before=datetime(2025, 1, 1),
                older_than_days=7,
                delete_all=True,
            )
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_audit_delete_rejects_no_mode(self):
        """Audit delete rejects when no mode is provided."""
        from app.routers.audit import delete_audit_logs

        background_tasks = BackgroundTasks()
        with pytest.raises(HTTPException) as exc_info:
            await delete_audit_logs(
                background_tasks=background_tasks,
                profile_id=1,
                before=None,
                older_than_days=None,
                delete_all=False,
            )
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_loadbalance_delete_custom_days(self):
        from app.routers.loadbalance import delete_loadbalance_events

        background_tasks = BackgroundTasks()

        with patch(
            "app.routers.loadbalance.delete_loadbalance_events_in_background",
            new_callable=AsyncMock,
        ) as mock_delete_loadbalance_events:
            response = await delete_loadbalance_events(
                background_tasks=background_tasks,
                profile_id=1,
                before=None,
                older_than_days=45,
                delete_all=False,
            )
            await background_tasks()

        assert response.accepted is True
        mock_delete_loadbalance_events.assert_awaited_once_with(
            profile_id=1,
            before=None,
            older_than_days=45,
            delete_all=False,
        )

    @pytest.mark.asyncio
    async def test_loadbalance_delete_before_still_works(self):
        from datetime import datetime

        from app.routers.loadbalance import delete_loadbalance_events

        cutoff = datetime(2025, 1, 1)
        background_tasks = BackgroundTasks()

        with patch(
            "app.routers.loadbalance.delete_loadbalance_events_in_background",
            new_callable=AsyncMock,
        ) as mock_delete_loadbalance_events:
            response = await delete_loadbalance_events(
                background_tasks=background_tasks,
                profile_id=1,
                before=cutoff,
                older_than_days=None,
                delete_all=False,
            )
            await background_tasks()

        assert response.accepted is True
        _, call_kwargs = cast(
            tuple[tuple[object, ...], dict[str, object]],
            mock_delete_loadbalance_events.await_args_list[0],
        )
        normalized_before = cast(datetime, call_kwargs["before"])
        assert normalized_before.tzinfo is not None

    @pytest.mark.asyncio
    async def test_loadbalance_delete_rejects_multiple_modes(self):
        from datetime import datetime

        from app.routers.loadbalance import delete_loadbalance_events

        background_tasks = BackgroundTasks()

        with pytest.raises(HTTPException) as exc_info:
            await delete_loadbalance_events(
                background_tasks=background_tasks,
                profile_id=1,
                before=datetime(2025, 1, 1),
                older_than_days=7,
                delete_all=False,
            )
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_loadbalance_delete_rejects_no_mode(self):
        from app.routers.loadbalance import delete_loadbalance_events

        background_tasks = BackgroundTasks()

        with pytest.raises(HTTPException) as exc_info:
            await delete_loadbalance_events(
                background_tasks=background_tasks,
                profile_id=1,
                before=None,
                older_than_days=None,
                delete_all=False,
            )
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_audit_list_filters_by_request_log_id(self):
        from app.routers.audit import list_audit_logs

        row = MagicMock()
        row.id = 9
        row.request_log_id = 321
        row.vendor_id = 1
        row.profile_id = 1
        row.model_id = "gpt-4o-mini"
        row.endpoint_id = 5
        row.connection_id = 8
        row.endpoint_base_url = "https://api.openai.com"
        row.endpoint_description = "Primary endpoint"
        row.request_method = "POST"
        row.request_url = "https://api.openai.com/v1/responses"
        row.request_headers = "{}"
        row.request_body = '{"model":"gpt-4o-mini"}'
        row.response_status = 200
        row.is_stream = False
        row.duration_ms = 123
        row.created_at = "2026-03-08T00:00:00Z"

        count_result = MagicMock()
        count_result.scalar.return_value = 1

        rows_result = MagicMock()
        rows_result.scalars.return_value.all.return_value = [row]

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(side_effect=[count_result, rows_result])

        response = await list_audit_logs(
            db=mock_db,
            profile_id=1,
            request_log_id=321,
            limit=50,
            offset=0,
        )

        assert response.total == 1
        assert len(response.items) == 1
        assert response.items[0].request_log_id == 321


class TestDEF061_ConnectionResponseEndpointMapping:
    """DEF-061 (P1): ConnectionResponse should map endpoint_rel to endpoint."""

    def test_connection_response_maps_endpoint_rel_attribute(self):
        from datetime import datetime, timezone
        from types import SimpleNamespace
        from app.schemas.schemas import ConnectionResponse

        now = datetime.now(timezone.utc)
        endpoint = SimpleNamespace(
            id=3,
            profile_id=1,
            name="primary-endpoint",
            base_url="https://api.openai.com",
            api_key="sk-test",
            position=0,
            created_at=now,
            updated_at=now,
        )
        connection = SimpleNamespace(
            id=9,
            profile_id=1,
            model_config_id=2,
            endpoint_id=3,
            endpoint_rel=endpoint,
            is_active=True,
            priority=0,
            name="primary",
            auth_type=None,
            custom_headers=None,
            pricing_template_id=None,
            pricing_template_rel=None,
            health_status="unknown",
            health_detail=None,
            last_health_check=None,
            monitoring_probe_interval_seconds=300,
            created_at=now,
            updated_at=now,
        )

        response = ConnectionResponse.model_validate(connection, from_attributes=True)

        assert response.endpoint is not None
        assert response.endpoint.id == 3
        assert response.endpoint.name == "primary-endpoint"


class TestDEF076_BatchPageFetchRoutes:
    @pytest.mark.asyncio
    async def test_models_by_endpoints_route_preserves_endpoint_order(self):
        from app.routers.models import get_models_by_endpoints
        from app.schemas.schemas import EndpointModelsBatchRequest

        mock_db = AsyncMock()

        with patch(
            "app.routers.models.get_models_by_endpoints_for_profile",
            new_callable=AsyncMock,
        ) as mock_get_models_by_endpoints:
            mock_get_models_by_endpoints.return_value = {
                12: [],
                10: [],
            }
            response = await get_models_by_endpoints(
                body=EndpointModelsBatchRequest(endpoint_ids=[12, 10]),
                db=mock_db,
                profile_id=3,
            )

        assert [item.endpoint_id for item in response.items] == [12, 10]
        _, call_kwargs = cast(
            tuple[tuple[object, ...], dict[str, object]],
            mock_get_models_by_endpoints.await_args_list[0],
        )
        assert call_kwargs["profile_id"] == 3
        assert call_kwargs["endpoint_ids"] == [12, 10]


class TestDEF077_ThroughputRPMContract:
    @pytest.mark.asyncio
    async def test_throughput_route_normalizes_datetimes_and_returns_rpm_fields(self):
        from app.routers.stats import get_throughput

        mock_db = AsyncMock()
        aware_from = TestDEF058_StatsTimezoneFilterNormalization._aware_utc_datetime()
        aware_to = TestDEF058_StatsTimezoneFilterNormalization._aware_utc_datetime()

        with patch(
            "app.routers.stats.get_throughput_stats", new_callable=AsyncMock
        ) as mock_get_throughput_stats:
            mock_get_throughput_stats.return_value = {
                "average_rpm": 1.5,
                "peak_rpm": 3.0,
                "current_rpm": 2.0,
                "total_requests": 9,
                "time_window_seconds": 360.0,
                "buckets": [
                    {
                        "timestamp": aware_from.isoformat(),
                        "request_count": 3,
                        "rpm": 3.0,
                    }
                ],
            }
            response = await get_throughput(
                db=mock_db,
                profile_id=7,
                from_time=aware_from,
                to_time=aware_to,
            )

        assert response.average_rpm == 1.5
        assert response.peak_rpm == 3.0
        assert response.current_rpm == 2.0
        assert response.buckets[0].rpm == 3.0
        assert not hasattr(response, "average_tps")
        _, call_kwargs = cast(
            tuple[tuple[object, ...], dict[str, object]],
            mock_get_throughput_stats.await_args_list[0],
        )
        from_time: datetime = cast(datetime, call_kwargs["from_time"])
        to_time: datetime = cast(datetime, call_kwargs["to_time"])
        assert from_time == aware_from
        assert to_time == aware_to
        assert from_time.tzinfo is not None
        assert to_time.tzinfo is not None
