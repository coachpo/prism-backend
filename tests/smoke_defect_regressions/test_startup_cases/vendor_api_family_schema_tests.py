from datetime import datetime, timezone

from app.core.database import Base


class TestDEF080_VendorApiFamilySchemaContract:
    def test_model_config_contract_uses_vendor_id_and_api_family(self):
        from app.schemas.schemas import ModelConfigCreate, ModelConfigResponse

        payload = {
            "vendor_id": 1,
            "api_family": "openai",
            "model_id": "gpt-4.1",
            "model_type": "native",
            "loadbalance_strategy_id": 3,
            "is_enabled": True,
        }

        validated = ModelConfigCreate.model_validate(payload)
        create_fields = set(ModelConfigCreate.model_fields)
        response_fields = set(ModelConfigResponse.model_fields)
        assert validated.vendor_id == 1
        assert validated.api_family == "openai"
        assert {"vendor_id", "api_family"}.issubset(create_fields)
        assert {"vendor_id", "api_family", "vendor"}.issubset(response_fields)

    def test_request_log_contracts_split_list_and_detail_without_downgrading_dashboard(
        self,
    ):
        import app.schemas.schemas as schema_surface
        from app.schemas.schemas import (
            RequestLogDetailResponse,
            RequestLogListItemResponse,
            RequestLogResponse,
        )

        list_fields = set(RequestLogListItemResponse.model_fields)
        detail_fields = set(RequestLogDetailResponse.model_fields)
        dashboard_fields = set(RequestLogResponse.model_fields)

        assert schema_surface.RequestLogListItemResponse is RequestLogListItemResponse
        assert schema_surface.RequestLogDetailResponse is RequestLogDetailResponse
        assert schema_surface.RequestLogResponse is RequestLogResponse
        assert {
            "id",
            "created_at",
            "model_id",
            "resolved_target_model_id",
            "api_family",
            "vendor_name",
            "status_code",
            "response_time_ms",
            "is_stream",
            "total_tokens",
            "total_cost_user_currency_micros",
            "report_currency_symbol",
        }.issubset(list_fields)
        assert "endpoint_base_url" not in list_fields
        assert "pricing_snapshot_input" not in list_fields
        assert "proxy_api_key_name_snapshot" not in list_fields
        assert {
            "summary",
            "request",
            "routing",
            "usage",
            "costing",
            "pricing",
        }.issubset(detail_fields)
        assert {"api_family", "endpoint_base_url", "pricing_snapshot_input"}.issubset(
            dashboard_fields
        )

    def test_orm_metadata_uses_vendors_table(self):
        import app.models.models  # noqa: F401

        table_names = set(Base.metadata.tables)

        assert "vendors" in table_names
        assert "providers" not in table_names

    def test_schema_surface_reexports_vendor_contracts_and_api_family(self):
        import app.schemas.schemas as schema_surface
        from app.schemas.schemas import ApiFamily, VendorCreate, VendorResponse

        vendor_create_fields = set(VendorCreate.model_fields)
        vendor_response_fields = set(VendorResponse.model_fields)

        assert schema_surface.ApiFamily is ApiFamily
        assert schema_surface.VendorCreate is VendorCreate
        assert schema_surface.VendorResponse is VendorResponse
        created_vendor = VendorCreate.model_validate(
            {
                "key": "zai",
                "name": "Z.ai",
                "description": "Z.ai Open Platform",
                "icon_key": None,
            }
        )
        response_vendor = VendorResponse.model_validate(
            {
                "id": 1,
                "key": "zai",
                "name": "Z.ai",
                "description": "Z.ai Open Platform",
                "icon_key": None,
                "audit_enabled": False,
                "audit_capture_bodies": True,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        )

        assert "key" in vendor_create_fields
        assert "key" in vendor_response_fields
        assert "icon_key" in vendor_create_fields
        assert "icon_key" in vendor_response_fields
        assert created_vendor.icon_key is None
        assert response_vendor.icon_key is None

    def test_profile_bootstrap_contract_reexports_nullable_active_profile_and_limits(
        self,
    ):
        import app.schemas.schemas as schema_surface
        from app.schemas.schemas import ProfileBootstrapResponse

        fields = set(ProfileBootstrapResponse.model_fields)
        validated = ProfileBootstrapResponse.model_validate(
            {
                "profiles": [],
                "active_profile": None,
                "profile_limits": {"max_profiles": 10},
            }
        )

        assert schema_surface.ProfileBootstrapResponse is ProfileBootstrapResponse
        assert {"profiles", "active_profile", "profile_limits"}.issubset(fields)
        assert validated.active_profile is None
        assert validated.profile_limits.max_profiles == 10
