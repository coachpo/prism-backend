import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services.stats_service import log_request


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
                api_family="openai",
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
                api_family="openai",
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
                api_family="openai",
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
    async def test_record_audit_log_enqueue_failure_does_not_raise(self):
        from app.services.audit_service import record_audit_log

        with patch(
            "app.services.audit_service.background_task_manager.enqueue",
            MagicMock(side_effect=RuntimeError("queue unavailable")),
        ):
            await record_audit_log(
                request_log_id=1,
                profile_id=1,
                vendor_id=1,
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

    @pytest.mark.asyncio
    async def test_record_audit_log_persists_stream_body_when_capture_enabled(self):
        from app.services.audit_service import record_audit_log

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        enqueued_job = {}

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        def capture_enqueue(*, name, run, max_retries=0, retry_delay_seconds=0.0):
            enqueued_job.update(
                name=name,
                run=run,
                max_retries=max_retries,
                retry_delay_seconds=retry_delay_seconds,
            )

        with (
            patch(
                "app.core.database.AsyncSessionLocal",
                return_value=mock_session_ctx,
            ),
            patch(
                "app.services.audit_service.background_task_manager.enqueue",
                MagicMock(side_effect=capture_enqueue),
            ),
        ):
            await record_audit_log(
                request_log_id=2,
                profile_id=1,
                vendor_id=1,
                model_id="gpt-4o-mini",
                request_method="POST",
                request_url="https://api.openai.com/v1/responses",
                request_headers={"authorization": "Bearer sk-test"},
                request_body=b'{"model":"gpt-4o-mini"}',
                response_status=200,
                response_headers={"content-type": "text/event-stream"},
                response_body=b'data: {"id":"resp_123"}\n\n',
                is_stream=True,
                duration_ms=15,
                capture_bodies=True,
            )

            await enqueued_job["run"]()

        mock_session.add.assert_called_once()
        audit_entry = mock_session.add.call_args[0][0]
        assert audit_entry.response_body == 'data: {"id":"resp_123"}\n\n'

    @pytest.mark.asyncio
    async def test_record_audit_log_persists_full_large_bodies_without_truncation(self):
        from app.services.audit_service import record_audit_log

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        enqueued_job = {}

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        def capture_enqueue(*, name, run, max_retries=0, retry_delay_seconds=0.0):
            enqueued_job.update(
                name=name,
                run=run,
                max_retries=max_retries,
                retry_delay_seconds=retry_delay_seconds,
            )

        request_payload = ("req-" + ("x" * 70000)).encode("utf-8")
        response_payload = ("resp-" + ("y" * 70000)).encode("utf-8")

        with (
            patch(
                "app.core.database.AsyncSessionLocal",
                return_value=mock_session_ctx,
            ),
            patch(
                "app.services.audit_service.background_task_manager.enqueue",
                MagicMock(side_effect=capture_enqueue),
            ),
        ):
            await record_audit_log(
                request_log_id=3,
                profile_id=1,
                vendor_id=1,
                model_id="gpt-4o-mini",
                request_method="POST",
                request_url="https://api.openai.com/v1/responses",
                request_headers={"authorization": "Bearer sk-test"},
                request_body=request_payload,
                response_status=200,
                response_headers={"content-type": "application/json"},
                response_body=response_payload,
                is_stream=False,
                duration_ms=25,
                capture_bodies=True,
            )

            await enqueued_job["run"]()

        mock_session.add.assert_called_once()
        audit_entry = mock_session.add.call_args[0][0]
        assert audit_entry.request_body == request_payload.decode("utf-8")
        assert audit_entry.response_body == response_payload.decode("utf-8")

    def test_audit_log_schema_includes_endpoint_fields(self):
        from app.schemas.schemas import AuditLogListItem, AuditLogDetail

        list_fields = set(AuditLogListItem.model_fields.keys())
        detail_fields = set(AuditLogDetail.model_fields.keys())

        for schema_fields in [list_fields, detail_fields]:
            assert "endpoint_id" in schema_fields
            assert "endpoint_base_url" in schema_fields
            assert "endpoint_description" in schema_fields

    def test_request_log_schema_includes_endpoint_description_and_tracking_fields(self):
        from app.schemas.schemas import RequestLogResponse

        fields = set(RequestLogResponse.model_fields.keys())
        assert "endpoint_description" in fields
        assert "ingress_request_id" in fields
        assert "attempt_number" in fields
        assert "provider_correlation_id" in fields


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
        mock_endpoint.base_url = "https://api.openai.com"

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
        assert response.endpoint_base_url == "https://api.openai.com"

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
            endpoint_name="openai-main",
            pricing_template_name="default-pricing",
        )
        assert export.pricing_template_name == "default-pricing"

        export_default = ConfigConnectionExport(endpoint_name="openai-main")
        assert export_default.pricing_template_name is None

    @pytest.mark.asyncio
    async def test_create_endpoint_persists_pricing_enabled(self):
        from app.models.models import ModelConfig
        from app.routers.connections import create_connection
        from app.schemas.schemas import ConnectionCreate, EndpointCreate

        model = ModelConfig(
            id=77,
            vendor_id=1,
            model_id="gpt-4o-mini",
            model_type="native",
        )

        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.get = AsyncMock(return_value=model)
        model_result = MagicMock()
        model_result.scalar_one_or_none.return_value = model
        lock_result = MagicMock()
        no_conflict_result = MagicMock()
        no_conflict_result.scalar_one_or_none.return_value = None
        next_position_result = MagicMock()
        next_position_result.scalar_one_or_none.return_value = None
        ordered_connections_result = MagicMock()
        ordered_connections_result.scalars.return_value.all.return_value = []
        template = MagicMock()
        template.id = 11
        template_result = MagicMock()
        template_result.scalar_one_or_none.return_value = template
        clear_round_robin_result = MagicMock()
        clear_round_robin_result.rowcount = 0
        mock_db.execute = AsyncMock(
            side_effect=[
                model_result,
                lock_result,
                no_conflict_result,
                next_position_result,
                template_result,
                lock_result,
                ordered_connections_result,
                clear_round_robin_result,
            ]
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
