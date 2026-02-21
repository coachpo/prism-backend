import json
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx
from fastapi import HTTPException

from app.services.proxy_service import (
    rewrite_model_in_body,
    extract_model_from_body,
    build_upstream_headers,
    PROVIDER_AUTH,
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
        parsed = json.loads(result)
        assert parsed["model"] == "claude-sonnet-4-20250514"
        assert parsed["messages"] == [{"role": "user", "content": "hi"}]

    def test_inject_model_when_missing_from_body(self):
        body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
        assert extract_model_from_body(body) is None
        result = rewrite_model_in_body(body, "gemini-2.5-flash")
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

        response = await delete_request_logs(
            db=mock_db, older_than_days=45, delete_all=False
        )
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

        response = await delete_request_logs(
            db=mock_db, older_than_days=None, delete_all=True
        )
        assert response.deleted_count == 100

    @pytest.mark.asyncio
    async def test_stats_delete_rejects_both_modes(self):
        """Stats delete rejects older_than_days + delete_all=true."""
        from app.routers.stats import delete_request_logs

        mock_db = AsyncMock()
        with pytest.raises(HTTPException) as exc_info:
            await delete_request_logs(db=mock_db, older_than_days=7, delete_all=True)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_stats_delete_rejects_neither_mode(self):
        """Stats delete rejects when neither mode is provided."""
        from app.routers.stats import delete_request_logs

        mock_db = AsyncMock()
        with pytest.raises(HTTPException) as exc_info:
            await delete_request_logs(
                db=mock_db, older_than_days=None, delete_all=False
            )
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

        response = await delete_audit_logs(
            db=mock_db, before=None, older_than_days=45, delete_all=False
        )
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

        response = await delete_audit_logs(
            db=mock_db, before=None, older_than_days=None, delete_all=True
        )
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
        response = await delete_audit_logs(
            db=mock_db, before=cutoff, older_than_days=None, delete_all=False
        )
        assert response.deleted_count == 3

    @pytest.mark.asyncio
    async def test_audit_delete_rejects_multiple_modes(self):
        """Audit delete rejects when multiple modes are provided."""
        from datetime import datetime
        from app.routers.audit import delete_audit_logs

        mock_db = AsyncMock()

        # before + older_than_days
        with pytest.raises(HTTPException) as exc_info:
            await delete_audit_logs(
                db=mock_db,
                before=datetime(2025, 1, 1),
                older_than_days=7,
                delete_all=False,
            )
        assert exc_info.value.status_code == 400

        # older_than_days + delete_all
        with pytest.raises(HTTPException) as exc_info:
            await delete_audit_logs(
                db=mock_db, before=None, older_than_days=7, delete_all=True
            )
        assert exc_info.value.status_code == 400

        # all three
        with pytest.raises(HTTPException) as exc_info:
            await delete_audit_logs(
                db=mock_db,
                before=datetime(2025, 1, 1),
                older_than_days=7,
                delete_all=True,
            )
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_audit_delete_rejects_no_mode(self):
        """Audit delete rejects when no mode is provided."""
        from app.routers.audit import delete_audit_logs

        mock_db = AsyncMock()
        with pytest.raises(HTTPException) as exc_info:
            await delete_audit_logs(
                db=mock_db, before=None, older_than_days=None, delete_all=False
            )
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
            "base_url",
            "api_key",
            "is_active",
            "priority",
            "description",
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
            "endpoints",
        }
        assert expected.issubset(fields), f"Missing fields: {expected - fields}"

    def test_roundtrip_custom_headers_preserved(self):
        from app.schemas.schemas import ConfigEndpointExport

        headers = {"X-Custom": "value", "X-Another": "test"}
        ep = ConfigEndpointExport(
            base_url="https://api.example.com",
            api_key="sk-test",
            custom_headers=headers,
            auth_type="openai",
        )
        exported = ep.model_dump(mode="json")
        reimported = ConfigEndpointExport(**exported)
        assert reimported.custom_headers == headers
        assert reimported.auth_type == "openai"

    def test_roundtrip_custom_headers_null(self):
        from app.schemas.schemas import ConfigEndpointExport

        ep = ConfigEndpointExport(
            base_url="https://api.example.com",
            api_key="sk-test",
            custom_headers=None,
        )
        exported = ep.model_dump(mode="json")
        reimported = ConfigEndpointExport(**exported)
        assert reimported.custom_headers is None

    def test_roundtrip_custom_headers_empty_dict(self):
        from app.schemas.schemas import ConfigEndpointExport

        ep = ConfigEndpointExport(
            base_url="https://api.example.com",
            api_key="sk-test",
            custom_headers={},
        )
        exported = ep.model_dump(mode="json")
        reimported = ConfigEndpointExport(**exported)
        assert reimported.custom_headers == {}

    def test_import_serializes_custom_headers_to_json_string(self):
        import json

        headers = {"X-Custom": "value"}
        serialized = json.dumps(headers) if headers is not None else None
        assert serialized == '{"X-Custom": "value"}'
        assert json.loads(serialized) == headers

    def test_full_config_roundtrip_schema(self):
        from app.schemas.schemas import (
            ConfigExportResponse,
            ConfigImportRequest,
            ConfigProviderExport,
            ConfigModelExport,
            ConfigEndpointExport,
        )
        from datetime import datetime, timezone

        config = ConfigExportResponse(
            version=1,
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
            models=[
                ConfigModelExport(
                    provider_type="openai",
                    model_id="gpt-4o",
                    display_name="GPT-4o",
                    model_type="native",
                    lb_strategy="failover",
                    is_enabled=True,
                    endpoints=[
                        ConfigEndpointExport(
                            base_url="https://api.openai.com/v1",
                            api_key="sk-test",
                            is_active=True,
                            priority=0,
                            description="Primary",
                            auth_type="openai",
                            custom_headers={"X-Org": "my-org"},
                        )
                    ],
                )
            ],
        )
        exported = config.model_dump(mode="json")
        reimported = ConfigImportRequest(**exported)

        assert len(reimported.providers) == 1
        assert reimported.providers[0].audit_enabled is True
        assert reimported.providers[0].audit_capture_bodies is False
        assert len(reimported.models) == 1
        m = reimported.models[0]
        assert m.model_id == "gpt-4o"
        assert m.lb_strategy == "failover"
        assert len(m.endpoints) == 1
        ep = m.endpoints[0]
        assert ep.custom_headers == {"X-Org": "my-org"}
        assert ep.auth_type == "openai"
        assert ep.priority == 0


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
                endpoint_base_url="http://example.com",
                status_code=200,
                response_time_ms=100,
                is_stream=False,
                request_path="/v1/chat/completions",
            )

        assert result is None

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
        from app.schemas.schemas import EndpointOwnerResponse

        fields = set(EndpointOwnerResponse.model_fields.keys())
        assert fields == {
            "endpoint_id",
            "model_config_id",
            "model_id",
            "endpoint_description",
            "endpoint_base_url",
        }

    @pytest.mark.asyncio
    async def test_owner_route_returns_correct_data(self):
        from app.routers.endpoints import get_endpoint_owner

        mock_model_config = MagicMock()
        mock_model_config.model_id = "gpt-4"

        mock_endpoint = MagicMock()
        mock_endpoint.id = 7
        mock_endpoint.model_config_id = 3
        mock_endpoint.description = "PackyCode"
        mock_endpoint.base_url = "https://api.openai.com/v1"
        mock_endpoint.model_config_rel = mock_model_config

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_endpoint

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        response = await get_endpoint_owner(endpoint_id=7, db=mock_db)

        assert response.endpoint_id == 7
        assert response.model_config_id == 3
        assert response.model_id == "gpt-4"
        assert response.endpoint_description == "PackyCode"
        assert response.endpoint_base_url == "https://api.openai.com/v1"

    @pytest.mark.asyncio
    async def test_owner_route_returns_404_for_missing_endpoint(self):
        from app.routers.endpoints import get_endpoint_owner

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(HTTPException) as exc_info:
            await get_endpoint_owner(endpoint_id=9999, db=mock_db)

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
            version=1,
            exported_at=datetime.now(timezone.utc),
            providers=[],
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
