import asyncio
import logging

from app.models.models import RequestLog
from app.schemas.domains.stats import RequestLogResponse
from app.services.realtime import connection_manager

logger = logging.getLogger(__name__)


async def log_request(
    *,
    model_id: str,
    profile_id: int,
    provider_type: str,
    endpoint_id: int | None,
    connection_id: int | None,
    endpoint_base_url: str | None,
    status_code: int,
    response_time_ms: int,
    is_stream: bool,
    request_path: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    success_flag: bool | None = None,
    billable_flag: bool | None = None,
    priced_flag: bool | None = None,
    unpriced_reason: str | None = None,
    cache_read_input_tokens: int | None = None,
    cache_creation_input_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    input_cost_micros: int | None = None,
    output_cost_micros: int | None = None,
    cache_read_input_cost_micros: int | None = None,
    cache_creation_input_cost_micros: int | None = None,
    reasoning_cost_micros: int | None = None,
    total_cost_original_micros: int | None = None,
    total_cost_user_currency_micros: int | None = None,
    currency_code_original: str | None = None,
    report_currency_code: str | None = None,
    report_currency_symbol: str | None = None,
    fx_rate_used: str | None = None,
    fx_rate_source: str | None = None,
    pricing_snapshot_unit: str | None = None,
    pricing_snapshot_input: str | None = None,
    pricing_snapshot_output: str | None = None,
    pricing_snapshot_cache_read_input: str | None = None,
    pricing_snapshot_cache_creation_input: str | None = None,
    pricing_snapshot_reasoning: str | None = None,
    pricing_snapshot_missing_special_token_price_policy: str | None = None,
    pricing_config_version_used: int | None = None,
    error_detail: str | None = None,
    endpoint_description: str | None = None,
) -> int | None:
    from app.core.database import AsyncSessionLocal

    try:
        entry = RequestLog(
            profile_id=profile_id,
            model_id=model_id,
            provider_type=provider_type,
            endpoint_id=endpoint_id,
            connection_id=connection_id,
            endpoint_base_url=endpoint_base_url,
            status_code=status_code,
            response_time_ms=response_time_ms,
            is_stream=is_stream,
            request_path=request_path,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            success_flag=success_flag,
            billable_flag=billable_flag,
            priced_flag=priced_flag,
            unpriced_reason=unpriced_reason,
            cache_read_input_tokens=cache_read_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            reasoning_tokens=reasoning_tokens,
            input_cost_micros=input_cost_micros,
            output_cost_micros=output_cost_micros,
            cache_read_input_cost_micros=cache_read_input_cost_micros,
            cache_creation_input_cost_micros=cache_creation_input_cost_micros,
            reasoning_cost_micros=reasoning_cost_micros,
            total_cost_original_micros=total_cost_original_micros,
            total_cost_user_currency_micros=total_cost_user_currency_micros,
            currency_code_original=currency_code_original,
            report_currency_code=report_currency_code,
            report_currency_symbol=report_currency_symbol,
            fx_rate_used=fx_rate_used,
            fx_rate_source=fx_rate_source,
            pricing_snapshot_unit=pricing_snapshot_unit,
            pricing_snapshot_input=pricing_snapshot_input,
            pricing_snapshot_output=pricing_snapshot_output,
            pricing_snapshot_cache_read_input=pricing_snapshot_cache_read_input,
            pricing_snapshot_cache_creation_input=pricing_snapshot_cache_creation_input,
            pricing_snapshot_reasoning=pricing_snapshot_reasoning,
            pricing_snapshot_missing_special_token_price_policy=pricing_snapshot_missing_special_token_price_policy,
            pricing_config_version_used=pricing_config_version_used,
            error_detail=error_detail,
            endpoint_description=endpoint_description,
        )
        async with AsyncSessionLocal() as log_db:
            log_db.add(entry)
            await log_db.commit()
            await log_db.refresh(entry)

            try:
                serialized_entry = RequestLogResponse.model_validate(entry).model_dump(
                    mode="json"
                )
                await connection_manager.broadcast_to_profile(
                    profile_id=profile_id,
                    channel="dashboard",
                    message={
                        "type": "dashboard.update",
                        "request_log": serialized_entry,
                    },
                )
                await connection_manager.broadcast_to_profile(
                    profile_id=profile_id,
                    channel="request_logs",
                    message={
                        "type": "request_logs.new",
                        "request_log": serialized_entry,
                    },
                )
                await connection_manager.broadcast_to_profile(
                    profile_id=profile_id,
                    channel="statistics",
                    message={
                        "type": "statistics.new",
                        "request_log": serialized_entry,
                    },
                )
            except Exception:
                logger.exception(
                    "Failed to broadcast request-log payload; falling back to dirty signals"
                )
                try:
                    await connection_manager.broadcast_to_profile(
                        profile_id=profile_id,
                        channel="dashboard",
                        message={"type": "dashboard.dirty"},
                    )
                    await connection_manager.broadcast_to_profile(
                        profile_id=profile_id,
                        channel="request_logs",
                        message={"type": "request_logs.dirty"},
                    )
                    await connection_manager.broadcast_to_profile(
                        profile_id=profile_id,
                        channel="statistics",
                        message={"type": "statistics.dirty"},
                    )
                except Exception:
                    logger.debug(
                        "Failed to broadcast dirty fallback for request log (non-critical)"
                    )

            return entry.id
    except asyncio.CancelledError:
        logger.debug("Request logging cancelled")
        return None
    except Exception:
        logger.exception("Failed to log request")
        return None
