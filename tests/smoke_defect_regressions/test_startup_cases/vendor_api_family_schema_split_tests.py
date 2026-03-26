from app.core.database import Base


class TestDEF080_VendorApiFamilySchemaSplit:
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
        legacy_id_field = "provider" + "_id"
        legacy_field = "provider" + "_type"

        assert validated.vendor_id == 1
        assert validated.api_family == "openai"
        assert {"vendor_id", "api_family"}.issubset(create_fields)
        assert {"vendor_id", "api_family", "vendor"}.issubset(response_fields)
        assert legacy_id_field not in create_fields
        assert legacy_id_field not in response_fields
        assert legacy_field not in create_fields
        assert legacy_field not in response_fields
        assert "provider" not in response_fields

    def test_request_log_response_contract_uses_api_family(self):
        from app.schemas.schemas import RequestLogResponse

        fields = set(RequestLogResponse.model_fields)
        legacy_field = "provider" + "_type"

        assert "api_family" in fields
        assert legacy_field not in fields

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
        legacy_field = "provider" + "_type"
        legacy_provider_create = "Provider" + "Create"
        legacy_provider_response = "Provider" + "Response"

        assert schema_surface.ApiFamily is ApiFamily
        assert schema_surface.VendorCreate is VendorCreate
        assert schema_surface.VendorResponse is VendorResponse
        assert "key" in vendor_create_fields
        assert "key" in vendor_response_fields
        assert legacy_field not in vendor_create_fields
        assert legacy_field not in vendor_response_fields
        assert not hasattr(schema_surface, legacy_provider_create)
        assert not hasattr(schema_surface, legacy_provider_response)
