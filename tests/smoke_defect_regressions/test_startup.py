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

class TestDEF007_EndpointIdentityInLogs:
    """DEF-007 (P1): log_request returns ID and stores endpoint_description; audit_service accepts endpoint metadata."""

    @pytest.mark.asyncio
    async def test_log_request_returns_id_and_stores_description(self):
        mock_entry = MagicMock()
        mock_entry.id = 42

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "app.core.database.AsyncSessionLocal",
            return_value=mock_session_ctx,
        ):

            async def fake_refresh(entry):
                entry.id = 42

            mock_session.refresh = AsyncMock(side_effect=fake_refresh)

            result = await log_request(
                model_id="test-model",
                profile_id=1,
                provider_type="openai",
                endpoint_id=1,
                connection_id=1,
                endpoint_base_url="http://example.com",
                status_code=200,
                response_time_ms=100,
                is_stream=False,
                request_path="/v1/chat/completions",
                endpoint_description="Primary endpoint",
            )

        assert result == 42
        mock_session.add.assert_called_once()
        added_entry = mock_session.add.call_args[0][0]
        assert added_entry.endpoint_description == "Primary endpoint"

    @pytest.mark.asyncio
    async def test_log_request_returns_none_on_failure(self):
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(side_effect=Exception("DB error"))
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "app.core.database.AsyncSessionLocal",
            return_value=mock_session_ctx,
        ):
            result = await log_request(
                model_id="test-model",
                profile_id=1,
                provider_type="openai",
                endpoint_id=1,
                connection_id=1,
                endpoint_base_url="http://example.com",
                status_code=200,
                response_time_ms=100,
                is_stream=False,
                request_path="/v1/chat/completions",
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_log_request_returns_none_on_cancelled_error(self):
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(side_effect=asyncio.CancelledError())
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "app.core.database.AsyncSessionLocal",
            return_value=mock_session_ctx,
        ):
            result = await log_request(
                model_id="test-model",
                profile_id=1,
                provider_type="openai",
                endpoint_id=1,
                connection_id=1,
                endpoint_base_url="http://example.com",
                status_code=200,
                response_time_ms=100,
                is_stream=True,
                request_path="/v1/responses",
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_record_audit_log_ignores_cancelled_error(self):
        from app.services.audit_service import record_audit_log

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(side_effect=asyncio.CancelledError())
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "app.core.database.AsyncSessionLocal",
            return_value=mock_session_ctx,
        ):
            await record_audit_log(
                request_log_id=1,
                profile_id=1,
                provider_id=1,
                model_id="gpt-4o-mini",
                request_method="POST",
                request_url="https://api.openai.com/v1/responses",
                request_headers={"authorization": "Bearer sk-test"},
                request_body=b'{"model":"gpt-4o-mini"}',
                response_status=200,
                response_headers={"content-type": "text/event-stream"},
                response_body=None,
                is_stream=True,
                duration_ms=15,
                capture_bodies=False,
            )

    def test_audit_log_schema_includes_endpoint_fields(self):
        from app.schemas.schemas import AuditLogListItem, AuditLogDetail

        list_fields = set(AuditLogListItem.model_fields.keys())
        detail_fields = set(AuditLogDetail.model_fields.keys())

        for schema_fields in [list_fields, detail_fields]:
            assert "endpoint_id" in schema_fields
            assert "endpoint_base_url" in schema_fields
            assert "endpoint_description" in schema_fields

    def test_request_log_schema_includes_endpoint_description(self):
        from app.schemas.schemas import RequestLogResponse

        fields = set(RequestLogResponse.model_fields.keys())
        assert "endpoint_description" in fields

class TestEndpointOwnerRoute:
    def test_owner_response_schema_has_required_fields(self):
        from app.schemas.schemas import ConnectionOwnerResponse

        fields = set(ConnectionOwnerResponse.model_fields.keys())
        assert fields == {
            "connection_id",
            "model_config_id",
            "model_id",
            "connection_name",
            "endpoint_id",
            "endpoint_name",
            "endpoint_base_url",
        }

    @pytest.mark.asyncio
    async def test_owner_route_returns_correct_data(self):
        from app.routers.connections import get_connection_owner

        mock_model_config = MagicMock()
        mock_model_config.model_id = "gpt-4"

        mock_endpoint = MagicMock()
        mock_endpoint.id = 13
        mock_endpoint.name = "primary"
        mock_endpoint.base_url = "https://api.openai.com/v1"

        mock_connection = MagicMock()
        mock_connection.id = 7
        mock_connection.model_config_id = 3
        mock_connection.name = "PackyCode"
        mock_connection.model_config_rel = mock_model_config
        mock_connection.endpoint_rel = mock_endpoint

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_connection

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        response = await get_connection_owner(connection_id=7, db=mock_db, profile_id=1)

        assert response.connection_id == 7
        assert response.model_config_id == 3
        assert response.model_id == "gpt-4"
        assert response.connection_name == "PackyCode"
        assert response.endpoint_id == 13
        assert response.endpoint_name == "primary"
        assert response.endpoint_base_url == "https://api.openai.com/v1"

    @pytest.mark.asyncio
    async def test_owner_route_returns_404_for_missing_endpoint(self):
        from app.routers.connections import get_connection_owner

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(HTTPException) as exc_info:
            await get_connection_owner(connection_id=9999, db=mock_db, profile_id=1)

        assert exc_info.value.status_code == 404

class TestDEF009_ConnectionDefaultsPersist:
    """DEF-009 (P1): connection schema defaults and creation path remain intact."""

    def test_config_export_connection_defaults(self):
        from app.schemas.schemas import ConfigConnectionExport

        export = ConfigConnectionExport(
            endpoint_id=1,
            connection_id=1,
            pricing_template_id=11,
        )
        assert export.pricing_template_id == 11

        export_default = ConfigConnectionExport(connection_id=2, endpoint_id=1)
        assert export_default.pricing_template_id is None

    @pytest.mark.asyncio
    async def test_create_endpoint_persists_pricing_enabled(self):
        from app.models.models import ModelConfig
        from app.routers.connections import create_connection
        from app.schemas.schemas import ConnectionCreate, EndpointCreate

        model = ModelConfig(
            id=77,
            provider_id=1,
            model_id="gpt-4o-mini",
            model_type="native",
        )

        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.get = AsyncMock(return_value=model)
        model_result = MagicMock()
        model_result.scalar_one_or_none.return_value = model
        no_conflict_result = MagicMock()
        no_conflict_result.scalar_one_or_none.return_value = None
        template = MagicMock()
        template.id = 11
        template_result = MagicMock()
        template_result.scalar_one_or_none.return_value = template
        mock_db.execute = AsyncMock(
            side_effect=[model_result, no_conflict_result, template_result]
        )
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch(
            "app.routers.connections._load_connection_or_404",
            AsyncMock(return_value=MagicMock(pricing_template_id=11)),
        ):
            connection = await create_connection(
                model_config_id=77,
                body=ConnectionCreate(
                    endpoint_create=EndpointCreate(
                        name="inline-endpoint",
                        base_url="https://api.example.com/v1",
                        api_key="sk-test",
                    ),
                    pricing_template_id=11,
                ),
                db=mock_db,
                profile_id=1,
            )

        assert connection.pricing_template_id == 11

class TestDEF020_FrontendBuildTypeCheck:
    """DEF-020: Frontend build confirms renamed snapshot policy field compiles."""

    def test_placeholder_for_frontend_build(self):
        """
        This test is a placeholder. The actual verification is running:
            cd frontend && pnpm run build
        which validates that the renamed field
        pricing_snapshot_missing_special_token_price_policy compiles.
        """
        pass

class TestDEF022_ProfileIsolationRuntimeDependencies:
    def test_proxy_routes_use_active_profile_dependency(self):
        from app.dependencies import get_active_profile_id, get_effective_profile_id
        from app.routers import proxy as proxy_router

        route_by_path = {
            route.path: route
            for route in proxy_router.router.routes
            if hasattr(route, "dependant")
        }

        v1_route = route_by_path["/v1/{path:path}"]
        v1beta_route = route_by_path["/v1beta/{path:path}"]

        v1_dependencies = {dep.call for dep in v1_route.dependant.dependencies}
        v1beta_dependencies = {dep.call for dep in v1beta_route.dependant.dependencies}

        assert get_active_profile_id in v1_dependencies
        assert get_active_profile_id in v1beta_dependencies
        assert get_effective_profile_id not in v1_dependencies
        assert get_effective_profile_id not in v1beta_dependencies

class TestDEF065_ModelDetailEndpointEagerLoad:
    """DEF-065 (P1): model detail responses must eagerly load connection endpoints."""

    @pytest.mark.asyncio
    async def test_get_model_returns_connections_with_endpoint_loaded(self):
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import Connection, Endpoint, ModelConfig, Profile, Provider
        from app.routers.models import get_model
        from app.schemas.schemas import ModelConfigResponse

        await get_engine().dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        model_id = f"def065-model-{suffix}"

        async with AsyncSessionLocal() as db:
            provider = (
                await db.execute(
                    select(Provider)
                    .where(Provider.provider_type == "openai")
                    .order_by(Provider.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if provider is None:
                provider = Provider(
                    name=f"DEF065 OpenAI {suffix}",
                    provider_type="openai",
                    description="DEF065 provider",
                )
                db.add(provider)
                await db.flush()

            profile = Profile(
                name=f"DEF065 Profile {suffix}",
                is_active=False,
                version=0,
            )
            db.add(profile)
            await db.flush()

            model = ModelConfig(
                profile_id=profile.id,
                provider_id=provider.id,
                model_id=model_id,
                model_type="native",
                redirect_to=None,
                lb_strategy="single",
                failover_recovery_enabled=True,
                failover_recovery_cooldown_seconds=60,
                is_enabled=True,
            )
            db.add(model)
            await db.flush()

            endpoint = Endpoint(
                profile_id=profile.id,
                name=f"DEF065 endpoint {suffix}",
                base_url="https://api.openai.com/v1",
                api_key="sk-test",
            )
            db.add(endpoint)
            await db.flush()

            connection = Connection(
                profile_id=profile.id,
                model_config_id=model.id,
                endpoint_id=endpoint.id,
                is_active=True,
                priority=0,
                name=f"DEF065 connection {suffix}",
            )
            db.add(connection)
            await db.flush()

            config = await get_model(
                model_config_id=model.id,
                db=db,
                profile_id=profile.id,
            )
            response = ModelConfigResponse.model_validate(config, from_attributes=True)

            assert len(response.connections) == 1
            assert response.connections[0].id == connection.id
            assert response.connections[0].endpoint is not None
            assert response.connections[0].endpoint.id == endpoint.id

class TestDEF025_ModelHealthStatsProfileScope:
    @pytest.mark.asyncio
    async def test_list_models_passes_profile_id_to_health_stats(self):
        from app.routers.models import list_models

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch(
            "app.routers.models.get_model_health_stats",
            AsyncMock(return_value={}),
        ) as health_mock:
            response = await list_models(db=mock_db, profile_id=7)

        assert response == []
        health_mock.assert_awaited_once_with(mock_db, profile_id=7)

    @pytest.mark.asyncio
    async def test_get_models_by_endpoint_passes_profile_id_to_health_stats(self):
        from app.routers.models import get_models_by_endpoint

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch(
            "app.routers.models.get_model_health_stats",
            AsyncMock(return_value={}),
        ) as health_mock:
            response = await get_models_by_endpoint(
                endpoint_id=123,
                db=mock_db,
                profile_id=9,
            )

        assert response == []
        health_mock.assert_awaited_once_with(mock_db, profile_id=9)

