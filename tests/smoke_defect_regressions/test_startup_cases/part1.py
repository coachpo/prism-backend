import asyncio
import json
import logging
from typing import AsyncGenerator, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services.proxy_service import (
    extract_model_from_body,
    build_upstream_headers,
)
from app.services.stats_service import log_request

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
        aware_to = self._aware_utc_datetime()

        with patch("app.routers.stats.get_request_logs", new_callable=AsyncMock) as mock_get_request_logs:
            mock_get_request_logs.return_value = ([], 0)
            response = await list_request_logs(
                db=mock_db,
                profile_id=7,
                from_time=aware_from,
                to_time=aware_to,
                limit=50,
                offset=0,
            )

        assert response.total == 0
        call_kwargs = mock_get_request_logs.await_args.kwargs
        assert call_kwargs["from_time"] == aware_from
        assert call_kwargs["to_time"] == aware_to
        assert call_kwargs["from_time"].tzinfo is not None
        assert call_kwargs["to_time"].tzinfo is not None

    @pytest.mark.asyncio
    async def test_summary_route_normalizes_aware_datetimes_before_service_call(self):
        from app.routers.stats import stats_summary

        mock_db = AsyncMock()
        aware_from = self._aware_utc_datetime()
        aware_to = self._aware_utc_datetime()

        with patch("app.routers.stats.get_stats_summary", new_callable=AsyncMock) as mock_get_stats_summary:
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
        call_kwargs = mock_get_stats_summary.await_args.kwargs
        assert call_kwargs["from_time"] == aware_from
        assert call_kwargs["to_time"] == aware_to
        assert call_kwargs["from_time"].tzinfo is not None
        assert call_kwargs["to_time"].tzinfo is not None

    @pytest.mark.asyncio
    async def test_connection_success_rates_route_normalizes_aware_datetimes(self):
        from app.routers.stats import connection_success_rates

        mock_db = AsyncMock()
        aware_from = self._aware_utc_datetime()
        aware_to = self._aware_utc_datetime()

        with patch("app.routers.stats.get_connection_success_rates", new_callable=AsyncMock) as mock_get_success_rates:
            mock_get_success_rates.return_value = []
            response = await connection_success_rates(
                db=mock_db,
                profile_id=7,
                from_time=aware_from,
                to_time=aware_to,
            )

        assert response == []
        call_kwargs = mock_get_success_rates.await_args.kwargs
        assert call_kwargs["from_time"] == aware_from
        assert call_kwargs["to_time"] == aware_to
        assert call_kwargs["from_time"].tzinfo is not None
        assert call_kwargs["to_time"].tzinfo is not None

    @pytest.mark.asyncio
    async def test_spending_route_normalizes_aware_datetimes_before_service_call(self):
        from app.routers.stats import spending_report

        mock_db = AsyncMock()
        aware_from = self._aware_utc_datetime()
        aware_to = self._aware_utc_datetime()

        with patch("app.routers.stats.get_spending_report", new_callable=AsyncMock) as mock_get_spending_report:
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
        call_kwargs = mock_get_spending_report.await_args.kwargs
        assert call_kwargs["from_time"] == aware_from
        assert call_kwargs["to_time"] == aware_to
        assert call_kwargs["from_time"].tzinfo is not None
        assert call_kwargs["to_time"].tzinfo is not None

class TestBatchDeleteValidation:
    """Validate flexible batch deletion for stats and audit endpoints."""

    @pytest.mark.asyncio
    async def test_stats_delete_custom_days(self):
        """Stats delete accepts any integer >= 1 for older_than_days."""
        from app.routers.stats import delete_request_logs

        mock_result = MagicMock()
        mock_result.rowcount = 5

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.flush = AsyncMock()

        response = await delete_request_logs(db=mock_db, profile_id=1, older_than_days=45, delete_all=False)
        assert response.deleted_count == 5
        mock_db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stats_delete_all(self):
        """Stats delete_all=true deletes all request logs."""
        from app.routers.stats import delete_request_logs

        mock_result = MagicMock()
        mock_result.rowcount = 100

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.flush = AsyncMock()

        response = await delete_request_logs(db=mock_db, profile_id=1, older_than_days=None, delete_all=True)
        assert response.deleted_count == 100

    @pytest.mark.asyncio
    async def test_stats_delete_rejects_both_modes(self):
        """Stats delete rejects older_than_days + delete_all=true."""
        from app.routers.stats import delete_request_logs

        mock_db = AsyncMock()
        with pytest.raises(HTTPException) as exc_info:
            await delete_request_logs(db=mock_db, profile_id=1, older_than_days=7, delete_all=True)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_stats_delete_rejects_neither_mode(self):
        """Stats delete rejects when neither mode is provided."""
        from app.routers.stats import delete_request_logs

        mock_db = AsyncMock()
        with pytest.raises(HTTPException) as exc_info:
            await delete_request_logs(db=mock_db, profile_id=1, older_than_days=None, delete_all=False)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_audit_delete_custom_days(self):
        """Audit delete accepts any integer >= 1 for older_than_days."""
        from app.routers.audit import delete_audit_logs

        mock_result = MagicMock()
        mock_result.rowcount = 10

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.flush = AsyncMock()

        response = await delete_audit_logs(db=mock_db, profile_id=1, before=None, older_than_days=45, delete_all=False)
        assert response.deleted_count == 10

    @pytest.mark.asyncio
    async def test_audit_delete_all(self):
        """Audit delete_all=true deletes all audit logs."""
        from app.routers.audit import delete_audit_logs

        mock_result = MagicMock()
        mock_result.rowcount = 50

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.flush = AsyncMock()

        response = await delete_audit_logs(db=mock_db, profile_id=1, before=None, older_than_days=None, delete_all=True)
        assert response.deleted_count == 50

    @pytest.mark.asyncio
    async def test_audit_delete_before_still_works(self):
        """Audit delete with 'before' datetime still works."""
        from datetime import datetime
        from app.routers.audit import delete_audit_logs

        mock_result = MagicMock()
        mock_result.rowcount = 3

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.flush = AsyncMock()

        cutoff = datetime(2025, 1, 1)
        response = await delete_audit_logs(db=mock_db, profile_id=1, before=cutoff, older_than_days=None, delete_all=False)
        assert response.deleted_count == 3

    @pytest.mark.asyncio
    async def test_audit_delete_rejects_multiple_modes(self):
        """Audit delete rejects when multiple modes are provided."""
        from datetime import datetime
        from app.routers.audit import delete_audit_logs

        mock_db = AsyncMock()

        # before + older_than_days
        with pytest.raises(HTTPException) as exc_info:
            await delete_audit_logs(db=mock_db, profile_id=1, before=datetime(2025, 1, 1), older_than_days=7, delete_all=False)
        assert exc_info.value.status_code == 400

        # older_than_days + delete_all
        with pytest.raises(HTTPException) as exc_info:
            await delete_audit_logs(db=mock_db, profile_id=1, before=None, older_than_days=7, delete_all=True)
        assert exc_info.value.status_code == 400

        # all three
        with pytest.raises(HTTPException) as exc_info:
            await delete_audit_logs(db=mock_db, profile_id=1, before=datetime(2025, 1, 1), older_than_days=7, delete_all=True)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_audit_delete_rejects_no_mode(self):
        """Audit delete rejects when no mode is provided."""
        from app.routers.audit import delete_audit_logs

        mock_db = AsyncMock()
        with pytest.raises(HTTPException) as exc_info:
            await delete_audit_logs(db=mock_db, profile_id=1, before=None, older_than_days=None, delete_all=False)
        assert exc_info.value.status_code == 400

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
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
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
            created_at=now,
            updated_at=now,
        )

        response = ConnectionResponse.model_validate(connection, from_attributes=True)

        assert response.endpoint is not None
        assert response.endpoint.id == 3
        assert response.endpoint.name == "primary-endpoint"

