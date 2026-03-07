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

class TestDEF016_MissingSpecialPriceFailsClosed:
    """DEF-016: Missing special prices produce MISSING_PRICE_DATA instead of fallback pricing."""

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
        from app.models.models import Connection, Endpoint, PricingTemplate

        endpoint = Endpoint(
            name="def016-endpoint",
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            profile_id=1,
            position=0,
        )
        endpoint.id = 1
        pricing_template = PricingTemplate(
            profile_id=1,
            name="def016-template",
            pricing_unit="PER_1M",
            pricing_currency_code="USD",
            input_price=input_price,
            output_price=output_price,
            cached_input_price=cached_input_price,
            cache_creation_price=cache_creation_price,
            reasoning_price=reasoning_price,
            missing_special_token_price_policy=missing_special_token_price_policy,
            version=10,
        )
        pricing_template.id = 1
        connection = Connection(
            model_config_id=1,
            endpoint_id=1,
            pricing_template_id=pricing_template.id,
        )
        connection.id = 1
        connection.endpoint_rel = endpoint
        connection.pricing_template_rel = pricing_template
        return connection, pricing_template, endpoint

    def test_missing_special_prices_set_unpriced_reason(self):
        from app.services.costing_service import (
            CostingSettingsSnapshot,
            compute_cost_fields,
        )

        connection, pricing_template, endpoint = self._build_connection(
            input_price="2",
            output_price="4",
            cached_input_price=None,
            cache_creation_price=None,
            reasoning_price=None,
            missing_special_token_price_policy="MAP_TO_OUTPUT",
        )

        result = compute_cost_fields(
            connection=connection,
            pricing_template=pricing_template,
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

        assert result["priced_flag"] is False
        assert result["unpriced_reason"] == "MISSING_PRICE_DATA"

class TestDEF017_ExplicitSpecialPricesAreUsed:
    """DEF-017: Provided special prices are used directly."""

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
        from app.models.models import Connection, Endpoint, PricingTemplate

        endpoint = Endpoint(
            name="def017-endpoint",
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            profile_id=1,
            position=0,
        )
        endpoint.id = 1
        pricing_template = PricingTemplate(
            profile_id=1,
            name="def017-template",
            pricing_unit="PER_1M",
            pricing_currency_code="USD",
            input_price=input_price,
            output_price=output_price,
            cached_input_price=cached_input_price,
            cache_creation_price=cache_creation_price,
            reasoning_price=reasoning_price,
            missing_special_token_price_policy=missing_special_token_price_policy,
            version=10,
        )
        pricing_template.id = 1
        connection = Connection(
            model_config_id=1,
            endpoint_id=1,
            pricing_template_id=pricing_template.id,
        )
        connection.id = 1
        connection.endpoint_rel = endpoint
        connection.pricing_template_rel = pricing_template
        return connection, pricing_template, endpoint

    def test_explicit_special_prices_produce_costs(self):
        from app.services.costing_service import (
            CostingSettingsSnapshot,
            compute_cost_fields,
        )

        connection, pricing_template, endpoint = self._build_connection(
            input_price="2",
            output_price="4",
            cached_input_price="1",
            cache_creation_price="3",
            reasoning_price="5",
            missing_special_token_price_policy="ZERO_COST",
        )

        result = compute_cost_fields(
            connection=connection,
            pricing_template=pricing_template,
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

        assert result["cache_read_input_cost_micros"] == 500_000
        assert result["cache_creation_input_cost_micros"] == 1_500_000
        assert result["reasoning_cost_micros"] == 2_500_000

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

