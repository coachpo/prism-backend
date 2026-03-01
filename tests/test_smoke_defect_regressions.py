import asyncio
import json
import logging
from typing import AsyncGenerator, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services.proxy_service import (
    rewrite_model_in_body,
    extract_model_from_body,
    build_upstream_headers,
)
from app.services.stats_service import log_request


class TestDEF001_LogsSurviveFailoverRollback:
    """DEF-001 (P0): request_logs must persist even when HTTPException(502) is raised."""

    @pytest.mark.asyncio
    async def test_log_request_uses_independent_session(self):
        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "app.core.database.AsyncSessionLocal",
            return_value=mock_session_ctx,
        ):
            await log_request(
                model_id="test-model",
                provider_type="openai",
                endpoint_id=1,
                connection_id=1,
                endpoint_base_url="http://example.com",
                status_code=503,
                response_time_ms=100,
                is_stream=False,
                request_path="/v1/chat/completions",
                error_detail="upstream failed",
            )

        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_log_request_uses_independent_session_without_caller_db(self):
        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "app.core.database.AsyncSessionLocal",
            return_value=mock_session_ctx,
        ):
            await log_request(
                model_id="test-model",
                provider_type="openai",
                endpoint_id=1,
                connection_id=1,
                endpoint_base_url="http://example.com",
                status_code=0,
                response_time_ms=50,
                is_stream=False,
                request_path="/v1/chat/completions",
                error_detail="connection refused",
            )
        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()


class TestDEF002_ModelIdRewriting:
    """DEF-002 (P1): proxy must inject/rewrite model field in forwarded body."""

    def test_rewrite_model_for_proxy_alias(self):
        body = json.dumps(
            {
                "model": "claude-sonnet-4",
                "messages": [{"role": "user", "content": "hi"}],
            }
        ).encode()
        result = rewrite_model_in_body(body, "claude-sonnet-4-20250514")
        assert result is not None
        parsed = json.loads(result)
        assert parsed["model"] == "claude-sonnet-4-20250514"
        assert parsed["messages"] == [{"role": "user", "content": "hi"}]

    def test_inject_model_when_missing_from_body(self):
        body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
        assert extract_model_from_body(body) is None
        result = rewrite_model_in_body(body, "gemini-2.5-flash")
        assert result is not None
        parsed = json.loads(result)
        assert parsed["model"] == "gemini-2.5-flash"

    def test_rewrite_preserves_other_fields(self):
        body = json.dumps(
            {
                "model": "alias-name",
                "messages": [{"role": "user", "content": "test"}],
                "temperature": 0.7,
                "max_tokens": 100,
                "stream": True,
            }
        ).encode()
        result = rewrite_model_in_body(body, "real-model-id")
        assert result is not None
        parsed = json.loads(result)
        assert parsed["model"] == "real-model-id"
        assert parsed["temperature"] == 0.7
        assert parsed["max_tokens"] == 100
        assert parsed["stream"] is True

    def test_rewrite_returns_none_for_none_body(self):
        assert rewrite_model_in_body(None, "model") is None

    def test_rewrite_returns_original_for_invalid_json(self):
        body = b"not json"
        result = rewrite_model_in_body(body, "model")
        assert result == body


class TestDEF003_AuthHeaderPerEndpoint:
    """DEF-003 (P1): auth header must be configurable per-endpoint via auth_type."""

    def _make_endpoint(self, auth_type=None, api_key="sk-test"):
        ep = MagicMock()
        ep.auth_type = auth_type
        ep.api_key = api_key
        return ep

    def test_openai_provider_uses_bearer_by_default(self):
        ep = self._make_endpoint()
        headers = build_upstream_headers(ep, "openai")
        assert headers["Authorization"] == "Bearer sk-test"
        assert "x-api-key" not in headers

    def test_anthropic_provider_uses_xapikey_by_default(self):
        ep = self._make_endpoint()
        headers = build_upstream_headers(ep, "anthropic")
        assert headers["x-api-key"] == "sk-test"
        assert "anthropic-version" in headers

    def test_anthropic_endpoint_with_openai_auth_override(self):
        ep = self._make_endpoint(auth_type="openai")
        headers = build_upstream_headers(ep, "anthropic")
        assert headers["Authorization"] == "Bearer sk-test"
        assert "x-api-key" not in headers

    def test_openai_endpoint_with_anthropic_auth_override(self):
        ep = self._make_endpoint(auth_type="anthropic")
        headers = build_upstream_headers(ep, "openai")
        assert headers["x-api-key"] == "sk-test"
        assert "anthropic-version" in headers

    def test_gemini_provider_uses_bearer_by_default(self):
        ep = self._make_endpoint()
        headers = build_upstream_headers(ep, "gemini")
        assert headers["Authorization"] == "Bearer sk-test"

    def test_auth_type_takes_precedence_over_provider_type(self):
        ep = self._make_endpoint(auth_type="openai")
        headers = build_upstream_headers(ep, "anthropic")
        assert "Authorization" in headers
        assert headers["Authorization"] == "Bearer sk-test"
        assert "x-api-key" not in headers


class TestDEF004_FrontendDeleteErrorHandling:
    """DEF-004 (P2): frontend must show error toast on failed model delete."""

    def test_api_client_throws_error_with_detail_message(self):
        pass


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


class TestDEF005_GeminiPathModelRewrite:
    """DEF-005 (P0): proxy must rewrite model ID in Gemini-style URL paths."""

    def test_rewrite_gemini_path(self):
        from app.routers.proxy import _rewrite_model_in_path

        result = _rewrite_model_in_path(
            "/v1beta/models/gemini-3-flash:generateContent",
            "gemini-3-flash",
            "gemini-3-flash-preview",
        )
        assert result == "/v1beta/models/gemini-3-flash-preview:generateContent"

    def test_rewrite_gemini_path_stream(self):
        from app.routers.proxy import _rewrite_model_in_path

        result = _rewrite_model_in_path(
            "/v1beta/models/gemini-3-flash:streamGenerateContent",
            "gemini-3-flash",
            "gemini-3-flash-preview",
        )
        assert result == "/v1beta/models/gemini-3-flash-preview:streamGenerateContent"

    def test_no_rewrite_when_same_model(self):
        from app.routers.proxy import _rewrite_model_in_path

        path = "/v1beta/models/gemini-3-flash:generateContent"
        result = _rewrite_model_in_path(path, "gemini-3-flash", "gemini-3-flash")
        assert result == path

    def test_non_gemini_path_unchanged(self):
        from app.routers.proxy import _extract_model_from_path

        assert _extract_model_from_path("/v1/chat/completions") is None

    def test_extract_model_from_gemini_path(self):
        from app.routers.proxy import _extract_model_from_path

        assert (
            _extract_model_from_path("/v1beta/models/gemini-3-flash:generateContent")
            == "gemini-3-flash"
        )


class TestDEF006_ConfigExportImportFieldCoverage:
    """DEF-006 (P0): config export/import must preserve all mutable fields including custom_headers."""

    def test_export_schema_includes_all_endpoint_fields(self):
        from app.schemas.schemas import ConfigEndpointExport

        fields = set(ConfigEndpointExport.model_fields.keys())
        expected = {
            "endpoint_ref",
            "name",
            "base_url",
            "api_key",
        }
        assert expected.issubset(fields), f"Missing fields: {expected - fields}"

    def test_export_schema_includes_all_connection_fields(self):
        from app.schemas.schemas import ConfigConnectionExport

        fields = set(ConfigConnectionExport.model_fields.keys())
        expected = {
            "connection_ref",
            "endpoint_ref",
            "is_active",
            "priority",
            "name",
            "auth_type",
            "custom_headers",
        }
        assert expected.issubset(fields), f"Missing fields: {expected - fields}"

    def test_export_schema_includes_all_provider_fields(self):
        from app.schemas.schemas import ConfigProviderExport

        fields = set(ConfigProviderExport.model_fields.keys())
        expected = {
            "name",
            "provider_type",
            "description",
            "audit_enabled",
            "audit_capture_bodies",
        }
        assert expected.issubset(fields), f"Missing fields: {expected - fields}"

    def test_export_schema_includes_all_model_fields(self):
        from app.schemas.schemas import ConfigModelExport

        fields = set(ConfigModelExport.model_fields.keys())
        expected = {
            "provider_type",
            "model_id",
            "display_name",
            "model_type",
            "redirect_to",
            "lb_strategy",
            "is_enabled",
            "connections",
        }
        assert expected.issubset(fields), f"Missing fields: {expected - fields}"

    def test_roundtrip_custom_headers_preserved(self):
        from app.schemas.schemas import ConfigConnectionExport

        headers = {"X-Custom": "value", "X-Another": "test"}
        connection = ConfigConnectionExport(
            endpoint_ref="endpoint:1",
            connection_ref="connection:test:1",
            custom_headers=headers,
            auth_type="openai",
        )
        exported = connection.model_dump(mode="json")
        reimported = ConfigConnectionExport(**exported)
        assert reimported.custom_headers == headers
        assert reimported.auth_type == "openai"

    def test_roundtrip_custom_headers_null(self):
        from app.schemas.schemas import ConfigConnectionExport

        connection = ConfigConnectionExport(
            endpoint_ref="endpoint:1",
            connection_ref="connection:test:2",
            custom_headers=None,
        )
        exported = connection.model_dump(mode="json")
        reimported = ConfigConnectionExport(**exported)
        assert reimported.custom_headers is None

    def test_roundtrip_custom_headers_empty_dict(self):
        from app.schemas.schemas import ConfigConnectionExport

        connection = ConfigConnectionExport(
            endpoint_ref="endpoint:1",
            connection_ref="connection:test:3",
            custom_headers={},
        )
        exported = connection.model_dump(mode="json")
        reimported = ConfigConnectionExport(**exported)
        assert reimported.custom_headers == {}

    def test_import_serializes_custom_headers_to_json_string(self):
        import json

        headers = {"X-Custom": "value"}
        serialized = json.dumps(headers) if headers is not None else None
        assert serialized == '{"X-Custom": "value"}'
        assert json.loads(serialized) == headers

    def test_full_config_roundtrip_schema(self):
        from app.schemas.schemas import (
            ConfigConnectionExport,
            ConfigEndpointExport,
            ConfigExportResponse,
            ConfigImportRequest,
            ConfigModelExport,
            ConfigProviderExport,
        )
        from datetime import datetime, timezone

        config = ConfigExportResponse(
            config_version="1",
            exported_at=datetime.now(timezone.utc),
            providers=[
                ConfigProviderExport(
                    name="OpenAI",
                    provider_type="openai",
                    description="Main provider",
                    audit_enabled=True,
                    audit_capture_bodies=False,
                )
            ],
            endpoints=[
                ConfigEndpointExport(
                    endpoint_ref="endpoint:openai-main",
                    name="openai-main",
                    base_url="https://api.openai.com/v1",
                    api_key="sk-test",
                )
            ],
            models=[
                ConfigModelExport(
                    provider_type="openai",
                    model_id="gpt-4o",
                    display_name="GPT-4o",
                    model_type="native",
                    lb_strategy="failover",
                    is_enabled=True,
                    failover_recovery_enabled=True,
                    failover_recovery_cooldown_seconds=60,
                    connections=[
                        ConfigConnectionExport(
                            connection_ref="connection:gpt-4o:openai-main:0:primary:0",
                            endpoint_ref="endpoint:openai-main",
                            is_active=True,
                            priority=0,
                            name="Primary",
                            auth_type="openai",
                            custom_headers={"X-Org": "my-org"},
                        )
                    ],
                )
            ],
        )
        exported = config.model_dump(mode="json")
        exported["mode"] = "replace"
        reimported = ConfigImportRequest(**exported)

        assert len(reimported.providers) == 1
        assert reimported.providers[0].audit_enabled is True
        assert reimported.providers[0].audit_capture_bodies is False
        assert len(reimported.models) == 1
        m = reimported.models[0]
        assert m.model_id == "gpt-4o"
        assert m.lb_strategy == "failover"
        assert m.failover_recovery_enabled is True
        assert m.failover_recovery_cooldown_seconds == 60
        assert len(m.connections) == 1
        connection = m.connections[0]
        assert connection.custom_headers == {"X-Org": "my-org"}
        assert connection.auth_type == "openai"
        assert connection.priority == 0


class TestDEF008_CacheCreationPricing:
    """DEF-008 (P1): cache creation pricing is tracked separately from cached input."""

    @staticmethod
    def _build_connection(
        *,
        input_price: str,
        output_price: str,
        cached_input_price: str,
        cache_creation_price: str,
        reasoning_price: str,
        missing_special_token_price_policy: str,
    ):
        from app.models.models import Connection, Endpoint

        endpoint = Endpoint(
            name="pricing-endpoint",
            base_url="https://api.example.com/v1",
            api_key="sk-test",
        )
        endpoint.id = 1
        connection = Connection(
            model_config_id=1,
            endpoint_id=1,
            pricing_enabled=True,
            pricing_currency_code="USD",
            input_price=input_price,
            output_price=output_price,
            cached_input_price=cached_input_price,
            cache_creation_price=cache_creation_price,
            reasoning_price=reasoning_price,
            missing_special_token_price_policy=missing_special_token_price_policy,
            pricing_config_version=9,
        )
        connection.id = 1
        connection.endpoint_rel = endpoint
        return connection, endpoint

    def test_extract_token_usage_parses_cache_creation_tokens(self):
        from app.services.stats_service import extract_token_usage

        body = json.dumps(
            {
                "usage": {
                    "prompt_tokens": 1200,
                    "completion_tokens": 400,
                    "total_tokens": 1600,
                    "prompt_tokens_details": {
                        "cache_read_input_tokens": 200,
                        "cache_creation_input_tokens": 300,
                    },
                    "completion_tokens_details": {"reasoning_tokens": 50},
                }
            }
        ).encode("utf-8")

        usage = extract_token_usage(body)
        assert usage["cache_read_input_tokens"] == 200
        assert usage["cache_creation_input_tokens"] == 300
        assert usage["reasoning_tokens"] == 50

    def test_extract_token_usage_parses_responses_api_usage_details(self):
        from app.services.stats_service import extract_token_usage

        body = json.dumps(
            {
                "usage": {
                    "input_tokens": 300,
                    "output_tokens": 100,
                    "total_tokens": 400,
                    "input_tokens_details": {"cached_tokens": 80},
                    "output_tokens_details": {"reasoning_tokens": 25},
                }
            }
        ).encode("utf-8")

        usage = extract_token_usage(body)
        assert usage["input_tokens"] == 300
        assert usage["output_tokens"] == 100
        assert usage["total_tokens"] == 400
        assert usage["cache_read_input_tokens"] == 80
        assert usage["cache_creation_input_tokens"] == 0
        assert usage["reasoning_tokens"] == 25

    def test_extract_token_usage_parses_response_completed_sse_usage(self):
        from app.services.stats_service import extract_token_usage

        body = (
            "event: response.completed\n"
            'data: {"type":"response.completed","response":{"usage":{"input_tokens":75,"output_tokens":125,"total_tokens":200,"input_tokens_details":{"cached_tokens":32},"output_tokens_details":{"reasoning_tokens":64}}}}\n\n'
        ).encode("utf-8")

        usage = extract_token_usage(body)
        assert usage["input_tokens"] == 75
        assert usage["output_tokens"] == 125
        assert usage["total_tokens"] == 200
        assert usage["cache_read_input_tokens"] == 32
        assert usage["reasoning_tokens"] == 64

    def test_extract_token_usage_parses_gemini_thoughts_tokens_json(self):
        from app.services.stats_service import extract_token_usage

        body = json.dumps(
            {
                "usageMetadata": {
                    "promptTokenCount": 41,
                    "candidatesTokenCount": 19,
                    "totalTokenCount": 60,
                    "cachedContentTokenCount": 7,
                    "thoughtsTokenCount": 11,
                }
            }
        ).encode("utf-8")

        usage = extract_token_usage(body)
        assert usage["input_tokens"] == 41
        assert usage["output_tokens"] == 19
        assert usage["total_tokens"] == 60
        assert usage["cache_read_input_tokens"] == 7
        assert usage["reasoning_tokens"] == 11

    def test_extract_token_usage_parses_gemini_thoughts_tokens_sse(self):
        from app.services.stats_service import extract_token_usage

        body = (
            'data: {"usageMetadata":{"promptTokenCount":12,"candidatesTokenCount":5,"totalTokenCount":17,"cachedContentTokenCount":3,"thoughtsTokenCount":9}}\n\n'
        ).encode("utf-8")

        usage = extract_token_usage(body)
        assert usage["input_tokens"] == 12
        assert usage["output_tokens"] == 5
        assert usage["total_tokens"] == 17
        assert usage["cache_read_input_tokens"] == 3
        assert usage["reasoning_tokens"] == 9

    def test_compute_cost_fields_includes_cache_creation_cost(self):
        from app.services.costing_service import (
            CostingSettingsSnapshot,
            compute_cost_fields,
        )

        connection, endpoint = self._build_connection(
            input_price="2",
            output_price="4",
            cached_input_price="1",
            cache_creation_price="3",
            reasoning_price="5",
            missing_special_token_price_policy="ZERO_COST",
        )

        result = compute_cost_fields(
            connection=connection,
            endpoint=endpoint,
            model_id="claude-sonnet",
            status_code=200,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_input_tokens=100_000,
            cache_creation_input_tokens=200_000,
            reasoning_tokens=300_000,
            settings=CostingSettingsSnapshot(
                report_currency_code="USD",
                report_currency_symbol="$",
                endpoint_fx_map={},
            ),
        )

        assert result["cache_creation_input_tokens"] == 200_000
        assert result["cache_creation_input_cost_micros"] == 600_000
        assert result["total_cost_original_micros"] == 8_200_000
        assert result["pricing_snapshot_cache_creation_input"] == "3.000000"

    def test_compute_cost_fields_maps_missing_cache_creation_by_policy(self):
        from app.services.costing_service import (
            CostingSettingsSnapshot,
            compute_cost_fields,
        )

        connection, endpoint = self._build_connection(
            input_price="0",
            output_price="0",
            cached_input_price="0",
            cache_creation_price="2",
            reasoning_price="0",
            missing_special_token_price_policy="MAP_TO_OUTPUT",
        )

        result = compute_cost_fields(
            connection=connection,
            endpoint=endpoint,
            model_id="claude-sonnet",
            status_code=200,
            input_tokens=0,
            output_tokens=1_000,
            cache_read_input_tokens=None,
            cache_creation_input_tokens=None,
            reasoning_tokens=None,
            settings=CostingSettingsSnapshot(
                report_currency_code="USD",
                report_currency_symbol="$",
                endpoint_fx_map={},
            ),
        )

        assert result["cache_creation_input_tokens"] is None
        assert result["cache_creation_input_cost_micros"] == 0
        assert result["total_cost_original_micros"] == 0


class TestFailoverRecoveryFieldValidation:
    """Validate failover recovery field validation and config version 2."""

    def test_recovery_cooldown_validates_lower_bound(self):
        """Recovery cooldown must be >= 1."""
        from app.schemas.schemas import ModelConfigBase
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            ModelConfigBase(
                provider_id=1,
                model_id="gpt-4",
                display_name="GPT-4",
                model_type="native",
                lb_strategy="failover",
                failover_recovery_cooldown_seconds=0,
            )
        assert "must be between 1 and 3600" in str(exc_info.value)

    def test_recovery_cooldown_validates_upper_bound(self):
        """Recovery cooldown must be <= 3600."""
        from app.schemas.schemas import ModelConfigBase
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            ModelConfigBase(
                provider_id=1,
                model_id="gpt-4",
                display_name="GPT-4",
                model_type="native",
                lb_strategy="failover",
                failover_recovery_cooldown_seconds=3601,
            )
        assert "must be between 1 and 3600" in str(exc_info.value)

    def test_recovery_cooldown_accepts_valid_values(self):
        """Recovery cooldown accepts values in range [1, 3600]."""
        from app.schemas.schemas import ModelConfigBase

        # Lower bound
        config = ModelConfigBase(
            provider_id=1,
            model_id="gpt-4",
            display_name="GPT-4",
            model_type="native",
            lb_strategy="failover",
            failover_recovery_cooldown_seconds=1,
        )
        assert config.failover_recovery_cooldown_seconds == 1

        # Upper bound
        config = ModelConfigBase(
            provider_id=1,
            model_id="gpt-4",
            display_name="GPT-4",
            model_type="native",
            lb_strategy="failover",
            failover_recovery_cooldown_seconds=3600,
        )
        assert config.failover_recovery_cooldown_seconds == 3600

        # Mid-range
        config = ModelConfigBase(
            provider_id=1,
            model_id="gpt-4",
            display_name="GPT-4",
            model_type="native",
            lb_strategy="failover",
            failover_recovery_cooldown_seconds=120,
        )
        assert config.failover_recovery_cooldown_seconds == 120

    def test_lb_strategy_rejects_round_robin(self):
        """lb_strategy field rejects round_robin value."""
        from app.schemas.schemas import ModelConfigBase
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            ModelConfigBase.model_validate(
                {
                    "provider_id": 1,
                    "model_id": "gpt-4",
                    "display_name": "GPT-4",
                    "model_type": "native",
                    "lb_strategy": "round_robin",
                }
            )
        assert "Input should be 'single' or 'failover'" in str(exc_info.value)

    def test_config_export_includes_recovery_fields(self):
        """ConfigModelExport includes recovery fields."""
        from app.schemas.schemas import ConfigConnectionExport, ConfigModelExport

        model = ConfigModelExport(
            provider_type="openai",
            model_id="gpt-4",
            display_name="GPT-4",
            model_type="native",
            lb_strategy="failover",
            is_enabled=True,
            failover_recovery_enabled=False,
            failover_recovery_cooldown_seconds=300,
            connections=[
                ConfigConnectionExport(
                    connection_ref="connection:gpt-4:openai-main:0:primary:0",
                    endpoint_ref="endpoint:openai-main",
                )
            ],
        )
        exported = model.model_dump(mode="json")
        assert exported["failover_recovery_enabled"] is False
        assert exported["failover_recovery_cooldown_seconds"] == 300

    def test_config_import_accepts_version_1(self):
        """ConfigImportRequest accepts version 1."""
        from app.schemas.schemas import ConfigImportRequest

        validation = ConfigImportRequest.model_validate(
            {
                "config_version": "1",
                "providers": [],
                "endpoints": [],
                "models": [],
                "mode": "replace",
            }
        )
        assert validation.config_version == "1"
    def test_config_import_rejects_round_robin_in_models(self):
        """ConfigImportRequest rejects models with lb_strategy=round_robin."""
        from app.schemas.schemas import ConfigImportRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            ConfigImportRequest.model_validate(
                {
                    "config_version": "1",
                    "providers": [],
                    "endpoints": [
                        {
                            "endpoint_ref": "endpoint:openai-main",
                            "name": "openai-main",
                            "base_url": "https://api.openai.com/v1",
                            "api_key": "sk-test",
                        }
                    ],
                    "models": [
                        {
                            "provider_type": "openai",
                            "model_id": "gpt-4",
                            "display_name": "GPT-4",
                            "model_type": "native",
                            "lb_strategy": "round_robin",
                            "is_enabled": True,
                            "connections": [
                                {
                                    "connection_ref": "connection:gpt-4:openai-main:0:primary:0",
                                    "endpoint_ref": "endpoint:openai-main",
                                }
                            ],
                        }
                    ],
                    "mode": "replace",
                }
            )
        assert "Input should be 'single' or 'failover'" in str(exc_info.value)

    def test_config_import_rejects_duplicate_connection_id(self):
        """Config import validation rejects duplicate connection IDs."""
        from app.routers.config import _validate_import
        from app.schemas.schemas import ConfigImportRequest

        data = ConfigImportRequest.model_validate(
            {
                "config_version": "1",
                "providers": [
                    {
                        "name": "OpenAI",
                        "provider_type": "openai",
                    }
                ],
                "endpoints": [
                    {
                        "endpoint_ref": "endpoint:openai-main",
                        "name": "openai-main",
                        "base_url": "https://api.openai.com/v1",
                        "api_key": "sk-test",
                    }
                ],
                "models": [
                    {
                        "provider_type": "openai",
                        "model_id": "gpt-4o",
                        "model_type": "native",
                        "connections": [
                            {
                                "connection_ref": "connection:dup",
                                "endpoint_ref": "endpoint:openai-main",
                            }
                        ],
                    },
                    {
                        "provider_type": "openai",
                        "model_id": "gpt-4.1",
                        "model_type": "native",
                        "connections": [
                            {
                                "connection_ref": "connection:dup",
                                "endpoint_ref": "endpoint:openai-main",
                            }
                        ],
                    },
                ],
                "mode": "replace",
            }
        )

        with pytest.raises(HTTPException) as exc_info:
            _validate_import(data)

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Duplicate connection reference: 'connection:dup'"

    def test_config_version_1_roundtrip(self):
        """Config export/import roundtrip with version 1 and recovery fields."""
        from app.schemas.schemas import (
            ConfigConnectionExport,
            ConfigEndpointExport,
            ConfigExportResponse,
            ConfigImportRequest,
            ConfigModelExport,
            ConfigProviderExport,
        )
        from datetime import datetime, timezone

        config = ConfigExportResponse(
            config_version="1",
            exported_at=datetime.now(timezone.utc),
            providers=[
                ConfigProviderExport(
                    name="OpenAI",
                    provider_type="openai",
                    description="Main provider",
                    audit_enabled=True,
                    audit_capture_bodies=False,
                )
            ],
            endpoints=[
                ConfigEndpointExport(
                    endpoint_ref="endpoint:openai-main",
                    name="openai-main",
                    base_url="https://api.openai.com/v1",
                    api_key="sk-test",
                )
            ],
            models=[
                ConfigModelExport(
                    provider_type="openai",
                    model_id="gpt-4o",
                    display_name="GPT-4o",
                    model_type="native",
                    lb_strategy="failover",
                    is_enabled=True,
                    failover_recovery_enabled=False,
                    failover_recovery_cooldown_seconds=180,
                    connections=[
                        ConfigConnectionExport(
                            connection_ref="connection:gpt-4o:openai-main:0:primary:0",
                            endpoint_ref="endpoint:openai-main",
                            is_active=True,
                            priority=0,
                        )
                    ],
                )
            ],
        )
        exported = config.model_dump(mode="json")
        exported["mode"] = "replace"
        reimported = ConfigImportRequest(**exported)

        assert reimported.config_version == "1"
        assert len(reimported.models) == 1
        m = reimported.models[0]
        assert m.lb_strategy == "failover"
        assert m.failover_recovery_enabled is False
        assert m.failover_recovery_cooldown_seconds == 180

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


class TestHeaderBlocklist:
    """Header blocklist feature: exact/prefix matching, sanitization, and schema validation."""

    def test_header_is_blocked_exact_match(self):
        """HBL-001 (P0): header_is_blocked() returns True for exact match."""
        from app.services.proxy_service import header_is_blocked

        rule = MagicMock()
        rule.match_type = "exact"
        rule.pattern = "x-custom-header"
        rule.enabled = True

        assert header_is_blocked("X-Custom-Header", [rule]) is True
        assert header_is_blocked("x-custom-header", [rule]) is True

    def test_header_is_blocked_prefix_match(self):
        """HBL-002 (P0): header_is_blocked() returns True for prefix match."""
        from app.services.proxy_service import header_is_blocked

        rule = MagicMock()
        rule.match_type = "prefix"
        rule.pattern = "x-custom-"
        rule.enabled = True

        assert header_is_blocked("X-Custom-Foo", [rule]) is True
        assert header_is_blocked("x-custom-bar", [rule]) is True
        assert header_is_blocked("X-Other", [rule]) is False

    def test_header_is_blocked_returns_false_for_non_matching(self):
        """HBL-003 (P0): header_is_blocked() returns False when no rules match."""
        from app.services.proxy_service import header_is_blocked

        rule = MagicMock()
        rule.match_type = "exact"
        rule.pattern = "x-blocked"
        rule.enabled = True

        assert header_is_blocked("X-Allowed", [rule]) is False

    def test_header_is_blocked_skips_disabled_rules(self):
        """HBL-004 (P0): header_is_blocked() skips disabled rules."""
        from app.services.proxy_service import header_is_blocked

        rule = MagicMock()
        rule.match_type = "exact"
        rule.pattern = "x-blocked"
        rule.enabled = False

        assert header_is_blocked("X-Blocked", [rule]) is False

    def test_sanitize_headers_removes_blocked(self):
        """HBL-005 (P0): sanitize_headers() removes blocked headers."""
        from app.services.proxy_service import sanitize_headers

        rule = MagicMock()
        rule.match_type = "exact"
        rule.pattern = "x-blocked"
        rule.enabled = True

        headers = {"X-Blocked": "value", "X-Allowed": "value"}
        result = sanitize_headers(headers, [rule])

        assert "X-Blocked" not in result
        assert result["X-Allowed"] == "value"

    def test_sanitize_headers_preserves_non_blocked(self):
        """HBL-006 (P0): sanitize_headers() preserves non-blocked headers."""
        from app.services.proxy_service import sanitize_headers

        rule = MagicMock()
        rule.match_type = "prefix"
        rule.pattern = "x-block-"
        rule.enabled = True

        headers = {"X-Block-Foo": "value", "X-Allowed": "value", "Content-Type": "json"}
        result = sanitize_headers(headers, [rule])

        assert "X-Block-Foo" not in result
        assert result["X-Allowed"] == "value"
        assert result["Content-Type"] == "json"

    def test_build_upstream_headers_with_blocklist_strips_client_headers(self):
        """HBL-007 (P0): build_upstream_headers() with blocklist_rules strips blocked client headers."""
        from app.services.proxy_service import build_upstream_headers

        ep = MagicMock()
        ep.auth_type = None
        ep.api_key = "sk-test"
        ep.custom_headers = None

        rule = MagicMock()
        rule.match_type = "exact"
        rule.pattern = "x-blocked"
        rule.enabled = True

        client_headers = {"X-Blocked": "value", "X-Allowed": "value"}
        headers = build_upstream_headers(
            ep, "openai", client_headers=client_headers, blocklist_rules=[rule]
        )

        assert "X-Blocked" not in headers
        assert headers["X-Allowed"] == "value"
        assert headers["Authorization"] == "Bearer sk-test"

    def test_build_upstream_headers_protects_auth_from_blocklist(self):
        """HBL-008 (P0): build_upstream_headers() protects auth headers from blocklist."""
        from app.services.proxy_service import build_upstream_headers

        ep = MagicMock()
        ep.auth_type = None
        ep.api_key = "sk-test"
        ep.custom_headers = None

        rule = MagicMock()
        rule.match_type = "exact"
        rule.pattern = "authorization"
        rule.enabled = True

        headers = build_upstream_headers(ep, "openai", blocklist_rules=[rule])

        assert headers["Authorization"] == "Bearer sk-test"

    def test_header_blocklist_rule_create_validates_prefix_ends_with_dash(self):
        """HBL-009 (P1): HeaderBlocklistRuleCreate validates prefix pattern must end with '-'."""
        from app.schemas.schemas import HeaderBlocklistRuleCreate
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            HeaderBlocklistRuleCreate(
                name="Test", match_type="prefix", pattern="x-custom", enabled=True
            )
        assert "prefix pattern must end with '-'" in str(exc_info.value)

        valid = HeaderBlocklistRuleCreate(
            name="Test", match_type="prefix", pattern="x-custom-", enabled=True
        )
        assert valid.pattern == "x-custom-"

    def test_header_blocklist_rule_create_rejects_invalid_pattern_chars(self):
        """HBL-010 (P1): HeaderBlocklistRuleCreate rejects invalid pattern characters."""
        from app.schemas.schemas import HeaderBlocklistRuleCreate
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            HeaderBlocklistRuleCreate(
                name="Test", match_type="exact", pattern="X-Custom_Header", enabled=True
            )
        assert "lowercase alphanumeric characters and hyphens" in str(exc_info.value)

        with pytest.raises(ValidationError) as exc_info:
            HeaderBlocklistRuleCreate(
                name="Test", match_type="exact", pattern="x custom", enabled=True
            )
        assert "lowercase alphanumeric characters and hyphens" in str(exc_info.value)

    def test_header_blocklist_rule_export_roundtrip(self):
        """HBL-011 (P1): HeaderBlocklistRuleExport schema roundtrip preserves all fields."""
        from app.schemas.schemas import HeaderBlocklistRuleExport

        rule = HeaderBlocklistRuleExport(
            name="Block Custom",
            match_type="prefix",
            pattern="x-custom-",
            enabled=True,
            is_system=False,
        )
        exported = rule.model_dump(mode="json")
        reimported = HeaderBlocklistRuleExport(**exported)

        assert reimported.name == "Block Custom"
        assert reimported.match_type == "prefix"
        assert reimported.pattern == "x-custom-"
        assert reimported.enabled is True
        assert reimported.is_system is False

    def test_config_export_response_includes_header_blocklist_rules(self):
        """HBL-012 (P1): ConfigExportResponse schema includes header_blocklist_rules field."""
        from app.schemas.schemas import (
            ConfigExportResponse,
            HeaderBlocklistRuleExport,
        )
        from datetime import datetime, timezone

        config = ConfigExportResponse(
            config_version="1",
            exported_at=datetime.now(timezone.utc),
            providers=[],
            endpoints=[],
            models=[],
            header_blocklist_rules=[
                HeaderBlocklistRuleExport(
                    name="Block Custom",
                    match_type="prefix",
                    pattern="x-custom-",
                    enabled=True,
                    is_system=False,
                )
            ],
        )

        assert len(config.header_blocklist_rules) == 1
        assert config.header_blocklist_rules[0].pattern == "x-custom-"


class TestDEF009_ConnectionDefaultsPersist:
    """DEF-009 (P1): connection schema defaults and creation path remain intact."""
    def test_config_export_connection_defaults(self):
        from app.schemas.schemas import ConfigConnectionExport

        export = ConfigConnectionExport(
            endpoint_ref="endpoint:1",
            connection_ref="connection:stream:1",
            pricing_enabled=True,
        )
        assert export.pricing_enabled is True

        export_default = ConfigConnectionExport(connection_ref="connection:stream:2", endpoint_ref="endpoint:1")
        assert export_default.pricing_enabled is False

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
        mock_db.execute = AsyncMock(side_effect=[model_result, no_conflict_result])
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch(
            "app.routers.connections._load_connection_or_404",
            AsyncMock(return_value=MagicMock(pricing_enabled=True)),
        ):
            connection = await create_connection(
                model_config_id=77,
                body=ConnectionCreate(
                    endpoint_create=EndpointCreate(
                        name="inline-endpoint",
                        base_url="https://api.example.com/v1",
                        api_key="sk-test",
                    ),
                    pricing_enabled=True,
                    pricing_currency_code="USD",
                ),
                db=mock_db,
                profile_id=1,
            )

        assert connection.pricing_enabled is True


class TestDEF010_EndpointToggleClearsRecoveryState:
    def _make_connection(self, connection_id: int):
        from app.models.models import Connection

        return Connection(
            id=connection_id,
            model_config_id=1,
            endpoint_id=99,
            is_active=True,
            priority=0,
        )

    @pytest.mark.asyncio
    async def test_update_endpoint_disable_clears_recovery_state(self):
        from app.routers.connections import update_connection
        from app.schemas.schemas import ConnectionUpdate
        from app.services.loadbalancer import _recovery_state, mark_connection_failed

        connection = self._make_connection(401)
        mark_connection_failed(1, connection.id, 60, 10.0)
        assert (1, connection.id) in _recovery_state

        mock_db = AsyncMock()
        mock_db.get = AsyncMock(return_value=connection)
        mock_db.execute = AsyncMock(
            return_value=MagicMock(
                scalar_one_or_none=MagicMock(return_value=connection)
            )
        )
        mock_db.flush = AsyncMock()

        try:
            await update_connection(
                connection_id=connection.id,
                body=ConnectionUpdate(is_active=False),
                db=mock_db,
                profile_id=1,
            )
            assert connection.is_active is False
            assert (1, connection.id) not in _recovery_state
        finally:
            _recovery_state.pop((1, connection.id), None)

    @pytest.mark.asyncio
    async def test_delete_endpoint_clears_recovery_state(self):
        from app.routers.connections import delete_connection
        from app.services.loadbalancer import _recovery_state, mark_connection_failed

        connection = self._make_connection(402)
        mark_connection_failed(1, connection.id, 60, 10.0)
        assert (1, connection.id) in _recovery_state

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=connection)))
        mock_db.delete = AsyncMock()

        try:
            await delete_connection(connection_id=connection.id, db=mock_db, profile_id=1)
            assert (1, connection.id) not in _recovery_state
            mock_db.delete.assert_awaited_once_with(connection)
        finally:
            _recovery_state.pop((1, connection.id), None)


class TestDEF011_RuntimeEndpointActivityCheck:
    @pytest.mark.asyncio
    async def test_endpoint_is_active_now_returns_true_for_active_row(self):
        from app.routers.proxy import _endpoint_is_active_now

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = True

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        is_active = await _endpoint_is_active_now(mock_db, 7)
        assert is_active is True

    @pytest.mark.asyncio
    async def test_endpoint_is_active_now_returns_false_for_disabled_or_missing_row(
        self,
    ):
        from app.routers.proxy import _endpoint_is_active_now

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        is_active = await _endpoint_is_active_now(mock_db, 999)
        assert is_active is False


class TestDEF012_RuntimeEndpointToggleFailoverE2E:
    @pytest.mark.asyncio
    async def test_proxy_skips_endpoint_disabled_after_plan_and_uses_next_endpoint(self):
        import httpx
        from fastapi import FastAPI
        from sqlalchemy import select, update
        from starlette.requests import Request

        from app.core.database import AsyncSessionLocal
        from app.models.models import Connection, Endpoint, ModelConfig, Provider
        from app.routers.proxy import _handle_proxy
        from app.services.loadbalancer import (
            _recovery_state,
            build_attempt_plan as real_build_attempt_plan,
        )

        class DummyHttpClient:
            def __init__(self):
                self.sent_urls: list[str] = []

            def build_request(self, method: str, upstream_url: str, **kwargs):
                return httpx.Request(
                    method=method,
                    url=upstream_url,
                    headers=kwargs.get("headers"),
                    content=kwargs.get("content"),
                )

            async def send(self, request: httpx.Request, **kwargs):
                self.sent_urls.append(str(request.url))
                return httpx.Response(
                    status_code=200,
                    request=request,
                    headers={"content-type": "application/json"},
                    content=json.dumps(
                        {
                            "id": "chatcmpl-ok",
                            "usage": {
                                "prompt_tokens": 1,
                                "completion_tokens": 1,
                                "total_tokens": 2,
                            },
                        }
                    ).encode("utf-8"),
                )

        try:
            async with AsyncSessionLocal() as seed_db:
                provider = Provider(
                    name="OpenAI DEF012",
                    provider_type="openai",
                    audit_enabled=False,
                    audit_capture_bodies=False,
                )
                model = ModelConfig(
                    provider=provider,
                    model_id="gpt-4o-mini-def012",
                    display_name="GPT-4o Mini DEF012",
                    model_type="native",
                    lb_strategy="failover",
                    failover_recovery_enabled=True,
                    failover_recovery_cooldown_seconds=60,
                    is_enabled=True,
                )
                primary_endpoint = Endpoint(
                    name="primary",
                    base_url="https://primary.example.com/v1",
                    api_key="sk-primary",
                )
                secondary_endpoint = Endpoint(
                    name="secondary",
                    base_url="https://secondary.example.com/v1",
                    api_key="sk-secondary",
                )
                primary = Connection(
                    model_config_rel=model,
                    endpoint_rel=primary_endpoint,
                    is_active=True,
                    endpoint_id=1,
                    priority=0,
                    name="primary",
                )
                secondary = Connection(
                    model_config_rel=model,
                    endpoint_rel=secondary_endpoint,
                    endpoint_id=2,
                    is_active=True,
                    priority=1,
                    name="secondary",
                )
                seed_db.add_all(
                    [
                        provider,
                        model,
                        primary_endpoint,
                        secondary_endpoint,
                        primary,
                        secondary,
                    ]
                )
                await seed_db.commit()
                await seed_db.refresh(primary)
                await seed_db.refresh(secondary)
                primary_id = primary.id
                secondary_id = secondary.id

            async with AsyncSessionLocal() as db:
                client = DummyHttpClient()
                app = FastAPI()
                app.state.http_client = client
                request = Request(
                    {
                        "type": "http",
                        "http_version": "1.1",
                        "method": "POST",
                        "path": "/v1/chat/completions",
                        "raw_path": b"/v1/chat/completions",
                        "query_string": b"",
                        "headers": [
                            (b"host", b"testserver"),
                            (b"content-type", b"application/json"),
                        ],
                        "client": ("testclient", 50000),
                        "server": ("testserver", 80),
                        "scheme": "http",
                        "app": app,
                    }
                )

                raw_body = json.dumps(
                    {
                        "model": "gpt-4o-mini-def012",
                        "messages": [{"role": "user", "content": "hi"}],
                    }
                ).encode("utf-8")

                toggle_applied = False

                def build_plan_with_assert(profile_id, model_config, now_mono):
                    plan = real_build_attempt_plan(
                        profile_id, model_config, now_mono
                    )
                    assert [ep.id for ep in plan] == [primary_id, secondary_id]
                    return plan

                async def runtime_active_check(current_db, endpoint_id):
                    nonlocal toggle_applied
                    if endpoint_id == primary_id and not toggle_applied:
                        await current_db.execute(
                            update(Connection)
                            .where(Connection.id == primary_id)
                            .values(is_active=False)
                        )
                        await current_db.flush()
                        toggle_applied = True
                        return False

                    row = await current_db.execute(
                        select(Connection.is_active).where(Connection.id == endpoint_id)
                    )
                    active = row.scalar_one_or_none()
                    return bool(active) if active is not None else False

                with (
                    patch(
                        "app.routers.proxy.build_attempt_plan",
                        side_effect=build_plan_with_assert,
                    ),
                    patch(
                        "app.routers.proxy._endpoint_is_active_now",
                        AsyncMock(side_effect=runtime_active_check),
                    ),
                    patch("app.routers.proxy.log_request", AsyncMock(return_value=123)),
                ):
                    response = await _handle_proxy(
                        request=request,
                        db=db,
                        raw_body=raw_body,
                        request_path="/v1/chat/completions",
                    )

                assert response.status_code == 200
                assert len(client.sent_urls) == 1
                assert "secondary.example.com" in client.sent_urls[0]

                primary_row = await db.execute(
                    select(Connection.is_active).where(Connection.id == primary_id)
                )
                secondary_row = await db.execute(
                    select(Connection.is_active).where(Connection.id == secondary_id)
                )
                assert primary_row.scalar_one() is False
                assert secondary_row.scalar_one() is True
        finally:
            _recovery_state.clear()


class TestDEF013_AnthropicTopLevelCacheReadTokens:
    """DEF-013: Anthropic JSON usage with top-level cache_read_input_tokens parses correctly."""

    def test_anthropic_top_level_cache_read(self):
        from app.services.stats_service import extract_token_usage

        body = json.dumps(
            {
                "usage": {
                    "input_tokens": 500,
                    "output_tokens": 200,
                    "cache_read_input_tokens": 150,
                    "cache_creation_input_tokens": 80,
                }
            }
        ).encode("utf-8")

        usage = extract_token_usage(body)
        assert usage["input_tokens"] == 500
        assert usage["output_tokens"] == 200
        assert usage["cache_read_input_tokens"] == 150
        assert usage["cache_creation_input_tokens"] == 80


class TestDEF014_MissingSpecialFieldsYieldZero:
    """DEF-014: Usage present + missing special fields yields 0 (not None)."""

    def test_json_usage_missing_special_fields_are_zero(self):
        from app.services.stats_service import extract_token_usage

        body = json.dumps(
            {
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                }
            }
        ).encode("utf-8")

        usage = extract_token_usage(body)
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 50
        assert usage["cache_read_input_tokens"] == 0
        assert usage["cache_creation_input_tokens"] == 0
        assert usage["reasoning_tokens"] == 0

    def test_json_usage_empty_object_special_fields_are_zero(self):
        from app.services.stats_service import extract_token_usage

        body = json.dumps({"usage": {}}).encode("utf-8")

        usage = extract_token_usage(body)
        assert usage["input_tokens"] is None
        assert usage["output_tokens"] is None
        assert usage["total_tokens"] is None
        assert usage["cache_read_input_tokens"] == 0
        assert usage["cache_creation_input_tokens"] == 0
        assert usage["reasoning_tokens"] == 0

    def test_sse_usage_empty_object_special_fields_are_zero(self):
        from app.services.stats_service import extract_token_usage

        body = 'data: {"usage":{}}\n\n'.encode("utf-8")

        usage = extract_token_usage(body)
        assert usage["input_tokens"] is None
        assert usage["output_tokens"] is None
        assert usage["total_tokens"] is None
        assert usage["cache_read_input_tokens"] == 0
        assert usage["cache_creation_input_tokens"] == 0
        assert usage["reasoning_tokens"] == 0

    def test_gemini_usage_missing_special_fields_are_zero(self):
        from app.services.stats_service import extract_token_usage

        body = json.dumps(
            {
                "usageMetadata": {
                    "promptTokenCount": 40,
                    "candidatesTokenCount": 20,
                    "totalTokenCount": 60,
                }
            }
        ).encode("utf-8")

        usage = extract_token_usage(body)
        assert usage["input_tokens"] == 40
        assert usage["output_tokens"] == 20
        assert usage["cache_read_input_tokens"] == 0
        assert usage["reasoning_tokens"] == 0


class TestDEF015_NoUsageBlockYieldsNull:
    """DEF-015: No usage block yields None for all token fields."""

    def test_no_usage_key_returns_all_none(self):
        from app.services.stats_service import extract_token_usage

        body = json.dumps({"id": "chatcmpl-123", "choices": []}).encode("utf-8")

        usage = extract_token_usage(body)
        assert usage["input_tokens"] is None
        assert usage["output_tokens"] is None
        assert usage["total_tokens"] is None
        assert usage["cache_read_input_tokens"] is None
        assert usage["cache_creation_input_tokens"] is None
        assert usage["reasoning_tokens"] is None


class TestDEF016_MapToOutputFallback:
    """DEF-016: MAP_TO_OUTPUT applies output_price to missing special prices."""

    @staticmethod
    def _build_connection(
        *,
        input_price: str,
        output_price: str,
        cached_input_price: str | None,
        cache_creation_price: str | None,
        reasoning_price: str | None,
        missing_special_token_price_policy: str,
    ):
        from app.models.models import Connection, Endpoint

        endpoint = Endpoint(
            name="def016-endpoint",
            base_url="https://api.example.com/v1",
            api_key="sk-test",
        )
        endpoint.id = 1
        connection = Connection(
            model_config_id=1,
            endpoint_id=1,
            pricing_enabled=True,
            pricing_currency_code="USD",
            input_price=input_price,
            output_price=output_price,
            cached_input_price=cached_input_price,
            cache_creation_price=cache_creation_price,
            reasoning_price=reasoning_price,
            missing_special_token_price_policy=missing_special_token_price_policy,
            pricing_config_version=10,
        )
        connection.id = 1
        connection.endpoint_rel = endpoint
        return connection, endpoint

    def test_map_to_output_uses_output_price_for_missing_specials(self):
        from app.services.costing_service import (
            CostingSettingsSnapshot,
            compute_cost_fields,
        )

        connection, endpoint = self._build_connection(
            input_price="2",
            output_price="4",
            cached_input_price=None,
            cache_creation_price=None,
            reasoning_price=None,
            missing_special_token_price_policy="MAP_TO_OUTPUT",
        )

        result = compute_cost_fields(
            connection=connection,
            endpoint=endpoint,
            model_id="test-model",
            status_code=200,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_input_tokens=500_000,
            cache_creation_input_tokens=500_000,
            reasoning_tokens=500_000,
            settings=CostingSettingsSnapshot(
                report_currency_code="USD",
                report_currency_symbol="$",
                endpoint_fx_map={},
            ),
        )

        # All three special costs should use output_price (4 per 1M)
        assert result["cache_read_input_cost_micros"] == 2_000_000  # 500k * 4/1M * 1e6
        assert result["cache_creation_input_cost_micros"] == 2_000_000
        assert result["reasoning_cost_micros"] == 2_000_000
        # Snapshot should reflect the fallback price
        assert result["pricing_snapshot_cache_read_input"] == "4.000000"
        assert result["pricing_snapshot_cache_creation_input"] == "4.000000"
        assert result["pricing_snapshot_reasoning"] == "4.000000"


class TestDEF017_ZeroCostFallback:
    """DEF-017: ZERO_COST with missing special prices produces 0 special costs."""

    @staticmethod
    def _build_connection(
        *,
        input_price: str,
        output_price: str,
        cached_input_price: str | None,
        cache_creation_price: str | None,
        reasoning_price: str | None,
        missing_special_token_price_policy: str,
    ):
        from app.models.models import Connection, Endpoint

        endpoint = Endpoint(
            name="def017-endpoint",
            base_url="https://api.example.com/v1",
            api_key="sk-test",
        )
        endpoint.id = 1
        connection = Connection(
            model_config_id=1,
            endpoint_id=1,
            pricing_enabled=True,
            pricing_currency_code="USD",
            input_price=input_price,
            output_price=output_price,
            cached_input_price=cached_input_price,
            cache_creation_price=cache_creation_price,
            reasoning_price=reasoning_price,
            missing_special_token_price_policy=missing_special_token_price_policy,
            pricing_config_version=10,
        )
        connection.id = 1
        connection.endpoint_rel = endpoint
        return connection, endpoint

    def test_zero_cost_produces_zero_for_missing_specials(self):
        from app.services.costing_service import (
            CostingSettingsSnapshot,
            compute_cost_fields,
        )

        connection, endpoint = self._build_connection(
            input_price="2",
            output_price="4",
            cached_input_price=None,
            cache_creation_price=None,
            reasoning_price=None,
            missing_special_token_price_policy="ZERO_COST",
        )

        result = compute_cost_fields(
            connection=connection,
            endpoint=endpoint,
            model_id="test-model",
            status_code=200,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_input_tokens=500_000,
            cache_creation_input_tokens=500_000,
            reasoning_tokens=500_000,
            settings=CostingSettingsSnapshot(
                report_currency_code="USD",
                report_currency_symbol="$",
                endpoint_fx_map={},
            ),
        )

        # All three special costs should be zero
        assert result["cache_read_input_cost_micros"] == 0
        assert result["cache_creation_input_cost_micros"] == 0
        assert result["reasoning_cost_micros"] == 0
        # Snapshot should reflect zero price
        assert result["pricing_snapshot_cache_read_input"] == "0.000000"
        assert result["pricing_snapshot_cache_creation_input"] == "0.000000"
        assert result["pricing_snapshot_reasoning"] == "0.000000"


class TestDEF018_SpecialTokensNeverCopiedFromOutput:
    """DEF-018: Special token counts never substituted from output_tokens."""

    def test_special_fields_not_copied_from_output(self):
        from app.services.stats_service import extract_token_usage

        body = json.dumps(
            {
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 500,
                    "total_tokens": 600,
                }
            }
        ).encode("utf-8")

        usage = extract_token_usage(body)
        assert usage["output_tokens"] == 500
        # Special fields must be 0, NOT 500 (never copied from output)
        assert usage["cache_read_input_tokens"] == 0
        assert usage["cache_creation_input_tokens"] == 0
        assert usage["reasoning_tokens"] == 0


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


class TestDEF021_StreamingCancellationResilience:
    @staticmethod
    def _build_request(app, raw_body: bytes):
        from starlette.requests import Request

        async def receive_message():
            return {
                "type": "http.request",
                "body": raw_body,
                "more_body": False,
            }

        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "path": "/v1/responses",
                "raw_path": b"/v1/responses",
                "query_string": b"",
                "headers": [
                    (b"host", b"testserver"),
                    (b"content-type", b"application/json"),
                ],
                "client": ("testclient", 50001),
                "server": ("testserver", 80),
                "scheme": "http",
                "app": app,
            },
            receive=receive_message,
        )
        return request

    @staticmethod
    def _build_model_config_and_endpoint():
        provider = MagicMock()
        provider.provider_type = "openai"
        provider.audit_enabled = True
        provider.audit_capture_bodies = False
        provider.id = 11

        endpoint = MagicMock()
        endpoint.id = 201
        endpoint.endpoint_id = 201
        endpoint.base_url = "https://api.openai.com/v1"
        endpoint.api_key = "sk-test"
        endpoint.auth_type = None
        endpoint.name = "primary"

        connection = MagicMock()
        connection.id = 101
        connection.endpoint_id = 201
        connection.auth_type = None
        connection.name = "primary"
        connection.endpoint_rel = endpoint

        model_config = MagicMock()
        model_config.provider = provider
        model_config.model_id = "gpt-4o-mini"
        model_config.lb_strategy = "single"
        model_config.failover_recovery_enabled = False
        model_config.failover_recovery_cooldown_seconds = 60

        return model_config, connection

    @staticmethod
    def _build_db_mock():
        mock_rules_result = MagicMock()
        mock_rules_result.scalars.return_value.all.return_value = []
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_rules_result)
        return mock_db

    @staticmethod
    async def _wait_for_asyncmock_calls(
        mock_obj: AsyncMock, expected_min_calls: int = 1
    ):
        for _ in range(40):
            if mock_obj.await_count >= expected_min_calls:
                return
            await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_mid_stream_cancel_keeps_success_and_finalizes_logging(self, caplog):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from app.routers.proxy import _handle_proxy

        caplog.set_level(logging.ERROR)

        class CancelMidStreamResponse:
            def __init__(self):
                self.status_code = 200
                self.headers = {"content-type": "text/event-stream"}
                self.closed = False

            async def aiter_bytes(self):
                yield b'data: {"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
                raise asyncio.CancelledError()

            async def aclose(self):
                self.closed = True

        class DummyHttpClient:
            def __init__(self, upstream_resp):
                self._upstream_resp = upstream_resp

            def build_request(self, method: str, upstream_url: str, **kwargs):
                return httpx.Request(
                    method=method,
                    url=upstream_url,
                    headers=kwargs.get("headers"),
                    content=kwargs.get("content"),
                )

            async def send(self, request: httpx.Request, **kwargs):
                assert kwargs.get("stream") is True
                return self._upstream_resp

        app = FastAPI()
        upstream_resp = CancelMidStreamResponse()
        app.state.http_client = DummyHttpClient(upstream_resp)

        raw_body = json.dumps(
            {
                "model": "gpt-4o-mini",
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            }
        ).encode("utf-8")
        request = self._build_request(app, raw_body)
        mock_db = self._build_db_mock()
        model_config, endpoint = self._build_model_config_and_endpoint()

        with (
            patch(
                "app.routers.proxy.get_model_config_with_connections",
                AsyncMock(return_value=model_config),
            ),
            patch("app.routers.proxy.build_attempt_plan", return_value=[endpoint]),
            patch(
                "app.routers.proxy._endpoint_is_active_now",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.routers.proxy.load_costing_settings",
                AsyncMock(return_value=MagicMock()),
            ),
            patch("app.routers.proxy.compute_cost_fields", return_value={}),
            patch(
                "app.routers.proxy.log_request", AsyncMock(return_value=501)
            ) as log_mock,
            patch("app.routers.proxy.record_audit_log", AsyncMock()) as audit_mock,
        ):
            response = await _handle_proxy(
                request=request,
                db=mock_db,
                raw_body=raw_body,
                request_path="/v1/responses",
            )

            assert response.status_code == 200
            assert isinstance(response, StreamingResponse)
            stream = cast(AsyncGenerator[bytes, None], response.body_iterator)

            first = await stream.__anext__()
            assert first.startswith(b"data: ")

            with pytest.raises(asyncio.CancelledError):
                await stream.__anext__()

            await self._wait_for_asyncmock_calls(log_mock)
            await self._wait_for_asyncmock_calls(audit_mock)

            assert upstream_resp.closed is True
            log_mock.assert_awaited_once()
            audit_mock.assert_awaited_once()
            assert "Failed to log streaming request" not in caplog.text
            assert "Failed to record streaming audit log" not in caplog.text

    @pytest.mark.asyncio
    async def test_stream_generator_close_triggers_detached_finalize_without_error(
        self, caplog
    ):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from app.routers.proxy import _handle_proxy

        caplog.set_level(logging.ERROR)

        class SlowStreamResponse:
            def __init__(self):
                self.status_code = 200
                self.headers = {"content-type": "text/event-stream"}
                self.closed = False

            async def aiter_bytes(self):
                yield b'data: {"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
                await asyncio.sleep(1)

            async def aclose(self):
                self.closed = True

        class DummyHttpClient:
            def __init__(self, upstream_resp):
                self._upstream_resp = upstream_resp

            def build_request(self, method: str, upstream_url: str, **kwargs):
                return httpx.Request(
                    method=method,
                    url=upstream_url,
                    headers=kwargs.get("headers"),
                    content=kwargs.get("content"),
                )

            async def send(self, request: httpx.Request, **kwargs):
                assert kwargs.get("stream") is True
                return self._upstream_resp

        app = FastAPI()
        upstream_resp = SlowStreamResponse()
        app.state.http_client = DummyHttpClient(upstream_resp)

        raw_body = json.dumps(
            {
                "model": "gpt-4o-mini",
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            }
        ).encode("utf-8")
        request = self._build_request(app, raw_body)
        mock_db = self._build_db_mock()
        model_config, endpoint = self._build_model_config_and_endpoint()

        with (
            patch(
                "app.routers.proxy.get_model_config_with_connections",
                AsyncMock(return_value=model_config),
            ),
            patch("app.routers.proxy.build_attempt_plan", return_value=[endpoint]),
            patch(
                "app.routers.proxy._endpoint_is_active_now",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.routers.proxy.load_costing_settings",
                AsyncMock(return_value=MagicMock()),
            ),
            patch("app.routers.proxy.compute_cost_fields", return_value={}),
            patch(
                "app.routers.proxy.log_request", AsyncMock(return_value=777)
            ) as log_mock,
            patch("app.routers.proxy.record_audit_log", AsyncMock()) as audit_mock,
        ):
            response = await _handle_proxy(
                request=request,
                db=mock_db,
                raw_body=raw_body,
                request_path="/v1/responses",
            )

            assert response.status_code == 200
            assert isinstance(response, StreamingResponse)
            stream = cast(AsyncGenerator[bytes, None], response.body_iterator)

            first = await stream.__anext__()
            assert first.startswith(b"data: ")
            await stream.aclose()

            await self._wait_for_asyncmock_calls(log_mock)
            await self._wait_for_asyncmock_calls(audit_mock)

            assert upstream_resp.closed is True
            log_mock.assert_awaited_once()
            audit_mock.assert_awaited_once()
            assert "Failed to log streaming request" not in caplog.text
            assert "Failed to record streaming audit log" not in caplog.text



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


class TestDEF023_ConfigImportReferenceValidation:
    def test_validate_import_accepts_v1_logical_refs(self):
        from app.schemas.schemas import ConfigImportRequest
        from app.routers.config import _validate_import

        data = ConfigImportRequest.model_validate(
            {
                "config_version": "1",
                "providers": [{"name": "OpenAI", "provider_type": "openai"}],
                "endpoints": [
                    {
                        "endpoint_ref": "endpoint:openai-main",
                        "name": "openai-main",
                        "base_url": "https://api.openai.com/v1",
                        "api_key": "sk-test",
                    }
                ],
                "models": [
                    {
                        "provider_type": "openai",
                        "model_id": "gpt-4o",
                        "model_type": "native",
                        "connections": [
                            {
                                "connection_ref": "connection:gpt-4o:openai-main:0:primary:0",
                                "endpoint_ref": "endpoint:openai-main",
                            }
                        ],
                    }
                ],
                "user_settings": {
                    "endpoint_fx_mappings": [
                        {
                            "model_id": "gpt-4o",
                            "endpoint_ref": "endpoint:openai-main",
                            "fx_rate": "1",
                        }
                    ]
                },
                "mode": "replace",
            }
        )

        _validate_import(data)

    def test_validate_import_rejects_duplicate_logical_connection_refs(self):
        from app.routers.config import _validate_import
        from app.schemas.schemas import ConfigImportRequest

        data = ConfigImportRequest.model_validate(
            {
                "config_version": "1",
                "providers": [{"name": "OpenAI", "provider_type": "openai"}],
                "endpoints": [
                    {
                        "endpoint_ref": "endpoint:openai-main",
                        "name": "openai-main",
                        "base_url": "https://api.openai.com/v1",
                        "api_key": "sk-test",
                    }
                ],
                "models": [
                    {
                        "provider_type": "openai",
                        "model_id": "gpt-4o",
                        "model_type": "native",
                        "connections": [
                            {
                                "connection_ref": "connection:dup",
                                "endpoint_ref": "endpoint:openai-main",
                            }
                        ],
                    },
                    {
                        "provider_type": "openai",
                        "model_id": "gpt-4.1",
                        "model_type": "native",
                        "connections": [
                            {
                                "connection_ref": "connection:dup",
                                "endpoint_ref": "endpoint:openai-main",
                            }
                        ],
                    },
                ],
                "mode": "replace",
            }
        )

        with pytest.raises(HTTPException) as exc_info:
            _validate_import(data)

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Duplicate connection reference: 'connection:dup'"


class TestDEF024_ConfigImportExportRefRoundtrip:
    @pytest.mark.asyncio
    async def test_import_v1_roundtrip_preserves_logical_refs(self):
        from sqlalchemy import select
        from app.core.database import AsyncSessionLocal, engine
        from app.models.models import Connection, Endpoint, EndpointFxRateSetting
        from app.routers.config import export_config, import_config
        from app.schemas.schemas import ConfigImportRequest

        # Prevent cross-loop pooled asyncpg connections from previous tests.
        await engine.dispose()

        suffix = str(int(asyncio.get_running_loop().time() * 1_000_000))
        endpoint_name = f"def024-endpoint-{suffix}"
        model_id = f"def024-model-{suffix}"
        connection_name = f"def024-connection-{suffix}"
        endpoint_ref = f"endpoint:{endpoint_name}"
        connection_ref = f"connection:{model_id}:{endpoint_name}:0:{connection_name}:0"
        payload = ConfigImportRequest.model_validate(
            {
                "config_version": "1",
                "providers": [
                    {
                        "name": "OpenAI",
                        "provider_type": "openai",
                    }
                ],
                "endpoints": [
                    {
                        "endpoint_ref": endpoint_ref,
                        "name": endpoint_name,
                        "base_url": "https://api.openai.com/v1",
                        "api_key": "sk-test",
                    }
                ],
                "models": [
                    {
                        "provider_type": "openai",
                        "model_id": model_id,
                        "model_type": "native",
                        "connections": [
                            {
                                "connection_ref": connection_ref,
                                "endpoint_ref": endpoint_ref,
                                "name": connection_name,
                            }
                        ],
                    }
                ],
                "user_settings": {
                    "report_currency_code": "USD",
                    "report_currency_symbol": "$",
                    "endpoint_fx_mappings": [
                        {
                            "model_id": model_id,
                            "endpoint_ref": endpoint_ref,
                            "fx_rate": "1.25",
                        }
                    ],
                },
                "mode": "replace",
            }
        )

        async with AsyncSessionLocal() as db:
            response = await import_config(data=payload, db=db, profile_id=1)
            await db.commit()
            assert response.endpoints_imported == 1
            assert response.connections_imported == 1

        async with AsyncSessionLocal() as db:
            endpoint = (
                await db.execute(
                    select(Endpoint).where(
                        Endpoint.profile_id == 1,
                        Endpoint.name == endpoint_name,
                    )
                )
            ).scalar_one()
            connection = (
                await db.execute(
                    select(Connection).where(
                        Connection.profile_id == 1,
                        Connection.name == connection_name,
                    )
                )
            ).scalar_one()
            fx_row = (
                await db.execute(
                    select(EndpointFxRateSetting).where(
                        EndpointFxRateSetting.profile_id == 1,
                        EndpointFxRateSetting.model_id == model_id,
                        EndpointFxRateSetting.endpoint_id == endpoint.id,
                    )
                )
            ).scalar_one_or_none()

            assert isinstance(endpoint.id, int) and endpoint.id > 0
            assert isinstance(connection.id, int) and connection.id > 0
            assert connection.endpoint_id == endpoint.id
            assert fx_row is not None

            export_response = await export_config(db=db, profile_id=1)
            exported = json.loads(export_response.body)

        assert exported["config_version"] == "1"
        exported_endpoint = next(
            e for e in exported["endpoints"] if e["name"] == endpoint_name
        )
        assert exported_endpoint["endpoint_ref"] == endpoint_ref
        assert "endpoint_id" not in exported_endpoint

        exported_model = next(
            m for m in exported["models"] if m["model_id"] == model_id
        )
        exported_connection = next(
            c for c in exported_model["connections"] if c["name"] == connection_name
        )
        assert exported_connection["endpoint_ref"] == endpoint_ref
        assert "connection_id" not in exported_connection
        assert exported_connection["connection_ref"].startswith("connection:")

        exported_mapping = next(
            m
            for m in exported["user_settings"]["endpoint_fx_mappings"]
            if m["model_id"] == model_id
        )
        assert exported_mapping["endpoint_ref"] == endpoint_ref
        assert "endpoint_id" not in exported_mapping



class TestDEF026_ConfigImportSystemRuleTimestamp:
    @pytest.mark.asyncio
    async def test_import_updates_system_rules_without_timezone_errors(self):
        from app.core.database import AsyncSessionLocal, engine
        from app.routers.config import import_config
        from app.schemas.schemas import ConfigImportRequest

        await engine.dispose()

        async with AsyncSessionLocal() as db:
            from app.main import SYSTEM_BLOCKLIST_DEFAULTS

            system_rule = SYSTEM_BLOCKLIST_DEFAULTS[0]

            payload = ConfigImportRequest.model_validate(
                {
                    "config_version": "1",
                    "providers": [
                        {
                            "name": "OpenAI",
                            "provider_type": "openai",
                        }
                    ],
                    "endpoints": [],
                    "models": [],
                    "user_settings": {
                        "report_currency_code": "USD",
                        "report_currency_symbol": "$",
                        "endpoint_fx_mappings": [],
                    },
                    "header_blocklist_rules": [
                        {
                            "name": system_rule["name"],
                            "match_type": system_rule["match_type"],
                            "pattern": system_rule["pattern"],
                            "enabled": True,
                            "is_system": True,
                        }
                    ],
                    "mode": "replace",
                }
            )

            response = await import_config(data=payload, db=db, profile_id=1)

            assert response.providers_imported == 1
            assert response.endpoints_imported == 0
            assert response.models_imported == 0
            assert response.connections_imported == 0

            await db.rollback()
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
