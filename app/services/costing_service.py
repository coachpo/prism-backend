from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Connection, Endpoint, EndpointFxRateSetting, UserSetting

SIX_DP = Decimal("0.000001")
MICRO_FACTOR = Decimal("1000000")
UNIT_FACTORS: dict[str, Decimal] = {
    "PER_1K": Decimal("1000"),
    "PER_1M": Decimal("1000000"),
}


@dataclass
class CostingSettingsSnapshot:
    report_currency_code: str
    report_currency_symbol: str
    endpoint_fx_map: dict[tuple[str, int], str]


class CostFieldPayload(TypedDict):
    success_flag: bool
    billable_flag: bool
    priced_flag: bool
    unpriced_reason: str | None
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None
    reasoning_tokens: int | None
    input_cost_micros: int
    output_cost_micros: int
    cache_read_input_cost_micros: int
    cache_creation_input_cost_micros: int
    reasoning_cost_micros: int
    total_cost_original_micros: int
    total_cost_user_currency_micros: int
    currency_code_original: str | None
    report_currency_code: str
    report_currency_symbol: str
    fx_rate_used: str
    fx_rate_source: str
    pricing_snapshot_unit: str | None
    pricing_snapshot_input: str | None
    pricing_snapshot_output: str | None
    pricing_snapshot_cache_read_input: str | None
    pricing_snapshot_cache_creation_input: str | None
    pricing_snapshot_reasoning: str | None
    pricing_snapshot_missing_special_token_price_policy: str | None
    pricing_config_version_used: int | None


def _normalize_decimal_string(value: Decimal) -> str:
    return f"{value.quantize(SIX_DP, rounding=ROUND_HALF_EVEN):f}"


def parse_decimal_value(value: str | Decimal | int | float | None) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid decimal value: {value}") from exc


def decimal_to_micros(value: Decimal) -> int:
    quantized = value.quantize(SIX_DP, rounding=ROUND_HALF_EVEN)
    return int((quantized * MICRO_FACTOR).to_integral_value(rounding=ROUND_HALF_EVEN))


def micros_to_decimal_string(value: int | None) -> str | None:
    if value is None:
        return None
    return _normalize_decimal_string(Decimal(value) / MICRO_FACTOR)


def _parse_non_negative(value: str | Decimal | int | float | None) -> Decimal:
    parsed = parse_decimal_value(value)
    if parsed < 0:
        raise ValueError("Negative values are not allowed")
    return parsed


async def load_costing_settings(
    db: AsyncSession,
    *,
    model_id: str,
    endpoint_ids: list[int],
) -> CostingSettingsSnapshot:
    settings_row = (
        await db.execute(select(UserSetting).order_by(UserSetting.id.asc()).limit(1))
    ).scalar_one_or_none()

    if settings_row is None:
        report_currency_code = "USD"
        report_currency_symbol = "$"
    else:
        report_currency_code = settings_row.report_currency_code
        report_currency_symbol = settings_row.report_currency_symbol

    endpoint_fx_map: dict[tuple[str, int], str] = {}
    if endpoint_ids:
        fx_rows = (
            (
                await db.execute(
                    select(EndpointFxRateSetting).where(
                        EndpointFxRateSetting.model_id == model_id,
                        EndpointFxRateSetting.endpoint_id.in_(endpoint_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        endpoint_fx_map = {
            (row.model_id, row.endpoint_id): row.fx_rate for row in fx_rows
        }

    return CostingSettingsSnapshot(
        report_currency_code=report_currency_code,
        report_currency_symbol=report_currency_symbol,
        endpoint_fx_map=endpoint_fx_map,
    )


def compute_cost_fields(
    *,
    connection: Connection | None,
    endpoint: Endpoint | None,
    model_id: str,
    status_code: int,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read_input_tokens: int | None,
    cache_creation_input_tokens: int | None,
    reasoning_tokens: int | None,
    settings: CostingSettingsSnapshot,
) -> CostFieldPayload:
    success_flag = 200 <= status_code < 300
    billable_flag = success_flag

    result: CostFieldPayload = {
        "success_flag": success_flag,
        "billable_flag": billable_flag,
        "priced_flag": False,
        "unpriced_reason": None,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "reasoning_tokens": reasoning_tokens,
        "input_cost_micros": 0,
        "output_cost_micros": 0,
        "cache_read_input_cost_micros": 0,
        "cache_creation_input_cost_micros": 0,
        "reasoning_cost_micros": 0,
        "total_cost_original_micros": 0,
        "total_cost_user_currency_micros": 0,
        "currency_code_original": None,
        "report_currency_code": settings.report_currency_code,
        "report_currency_symbol": settings.report_currency_symbol,
        "fx_rate_used": "1.000000",
        "fx_rate_source": "DEFAULT_1_TO_1",
        "pricing_snapshot_unit": None,
        "pricing_snapshot_input": None,
        "pricing_snapshot_output": None,
        "pricing_snapshot_cache_read_input": None,
        "pricing_snapshot_cache_creation_input": None,
        "pricing_snapshot_reasoning": None,
        "pricing_snapshot_missing_special_token_price_policy": None,
        "pricing_config_version_used": None,
    }

    if connection is None:
        if success_flag:
            result["unpriced_reason"] = "MISSING_CONNECTION"
        return result

    endpoint_id = connection.endpoint_id if connection.endpoint_id is not None else None
    if endpoint_id is None and endpoint is not None:
        endpoint_id = endpoint.id
    if endpoint_id is None:
        if success_flag:
            result["unpriced_reason"] = "MISSING_ENDPOINT"
        return result

    fx_key = (model_id, endpoint_id)
    fx_rate_str = settings.endpoint_fx_map.get(fx_key)
    try:
        fx_rate = (
            _parse_non_negative(fx_rate_str)
            if fx_rate_str is not None
            else Decimal("1")
        )
    except ValueError:
        fx_rate = Decimal("1")
        fx_rate_str = None

    if fx_rate <= 0:
        fx_rate = Decimal("1")
        fx_rate_str = None

    result["fx_rate_used"] = _normalize_decimal_string(fx_rate)
    result["fx_rate_source"] = (
        "ENDPOINT_SPECIFIC" if fx_rate_str is not None else "DEFAULT_1_TO_1"
    )

    if not success_flag:
        return result

    if not connection.pricing_enabled:
        result["unpriced_reason"] = "PRICING_DISABLED"
        return result

    if not connection.pricing_unit or connection.pricing_unit not in UNIT_FACTORS:
        result["unpriced_reason"] = "MISSING_PRICE_DATA"
        return result

    if not connection.pricing_currency_code:
        result["unpriced_reason"] = "MISSING_PRICE_DATA"
        return result

    try:
        input_price = _parse_non_negative(connection.input_price)
        output_price = _parse_non_negative(connection.output_price)
        _parse_non_negative(connection.cached_input_price)
        _parse_non_negative(connection.cache_creation_price)
        _parse_non_negative(connection.reasoning_price)
    except ValueError:
        result["unpriced_reason"] = "MISSING_PRICE_DATA"
        return result

    if (
        input_tokens is None
        and output_tokens is None
        and cache_read_input_tokens is None
        and cache_creation_input_tokens is None
        and reasoning_tokens is None
    ):
        result["unpriced_reason"] = "MISSING_TOKEN_USAGE"
        return result

    policy = connection.missing_special_token_price_policy or "MAP_TO_OUTPUT"
    input_count = max(input_tokens or 0, 0)
    output_count = max(output_tokens or 0, 0)

    # Cost counts: used for pricing math only (policy-derived)
    cached_cost_count = (
        max(cache_read_input_tokens, 0) if cache_read_input_tokens is not None else 0
    )
    cache_creation_cost_count = (
        max(cache_creation_input_tokens, 0)
        if cache_creation_input_tokens is not None
        else 0
    )
    reasoning_cost_count = (
        max(reasoning_tokens, 0) if reasoning_tokens is not None else 0
    )

    # Prices: use endpoint config, fall back via policy if None
    if connection.cached_input_price is not None:
        cached_price = Decimal(str(connection.cached_input_price))
    elif policy == "MAP_TO_OUTPUT":
        cached_price = output_price  # use output price as fallback
    else:  # ZERO_COST
        cached_price = Decimal("0")

    if connection.cache_creation_price is not None:
        cache_creation_price = Decimal(str(connection.cache_creation_price))
    elif policy == "MAP_TO_OUTPUT":
        cache_creation_price = output_price
    else:  # ZERO_COST
        cache_creation_price = Decimal("0")

    if connection.reasoning_price is not None:
        reasoning_price = Decimal(str(connection.reasoning_price))
    elif policy == "MAP_TO_OUTPUT":
        reasoning_price = output_price
    else:  # ZERO_COST
        reasoning_price = Decimal("0")

    factor = UNIT_FACTORS[connection.pricing_unit]
    input_cost = (Decimal(input_count) / factor) * input_price
    output_cost = (Decimal(output_count) / factor) * output_price
    cached_cost = (Decimal(cached_cost_count) / factor) * cached_price
    cache_creation_cost = (
        Decimal(cache_creation_cost_count) / factor
    ) * cache_creation_price
    reasoning_cost = (Decimal(reasoning_cost_count) / factor) * reasoning_price

    total_original = (
        input_cost + output_cost + cached_cost + cache_creation_cost + reasoning_cost
    )
    total_user_currency = total_original * fx_rate

    result.update(
        {
            "priced_flag": True,
            "unpriced_reason": None,
            "cache_read_input_tokens": cache_read_input_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "reasoning_tokens": reasoning_tokens,
            "input_cost_micros": decimal_to_micros(input_cost),
            "output_cost_micros": decimal_to_micros(output_cost),
            "cache_read_input_cost_micros": decimal_to_micros(cached_cost),
            "cache_creation_input_cost_micros": decimal_to_micros(cache_creation_cost),
            "reasoning_cost_micros": decimal_to_micros(reasoning_cost),
            "total_cost_original_micros": decimal_to_micros(total_original),
            "total_cost_user_currency_micros": decimal_to_micros(total_user_currency),
            "currency_code_original": connection.pricing_currency_code,
            "pricing_snapshot_unit": connection.pricing_unit,
            "pricing_snapshot_input": _normalize_decimal_string(input_price),
            "pricing_snapshot_output": _normalize_decimal_string(output_price),
            "pricing_snapshot_cache_read_input": _normalize_decimal_string(
                cached_price
            ),
            "pricing_snapshot_cache_creation_input": _normalize_decimal_string(
                cache_creation_price
            ),
            "pricing_snapshot_reasoning": _normalize_decimal_string(reasoning_price),
            "pricing_snapshot_missing_special_token_price_policy": policy,
            "pricing_config_version_used": connection.pricing_config_version,
        }
    )
    return result
