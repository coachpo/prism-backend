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
        from app.models.models import Connection, Endpoint, PricingTemplate

        endpoint = Endpoint(
            name="pricing-endpoint",
            base_url="https://api.example.com/v1",
            api_key="sk-test",
        )
        endpoint.id = 1
        pricing_template = PricingTemplate(
            profile_id=1,
            name="def008-template",
            pricing_unit="PER_1M",
            pricing_currency_code="USD",
            input_price=input_price,
            output_price=output_price,
            cached_input_price=cached_input_price,
            cache_creation_price=cache_creation_price,
            reasoning_price=reasoning_price,
            missing_special_token_price_policy=missing_special_token_price_policy,
            version=9,
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

        connection, pricing_template, endpoint = self._build_connection(
            input_price="0",
            output_price="0",
            cached_input_price="0",
            cache_creation_price="2",
            reasoning_price="0",
            missing_special_token_price_policy="MAP_TO_OUTPUT",
        )

        result = compute_cost_fields(
            connection=connection,
            pricing_template=pricing_template,
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

