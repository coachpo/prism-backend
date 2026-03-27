import asyncio
import logging

from sqlalchemy.exc import IntegrityError

from app.models.models import UsageRequestEvent

logger = logging.getLogger(__name__)


async def log_final_usage_request_event(
    *,
    model_id: str,
    profile_id: int,
    api_family: str,
    resolved_target_model_id: str | None = None,
    endpoint_id: int | None,
    connection_id: int | None,
    proxy_api_key_id: int | None = None,
    proxy_api_key_name_snapshot: str | None = None,
    ingress_request_id: str,
    status_code: int,
    success_flag: bool,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
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
    attempt_count: int,
    request_path: str,
) -> int | None:
    from app.core.database import AsyncSessionLocal

    try:
        entry = UsageRequestEvent(
            profile_id=profile_id,
            ingress_request_id=ingress_request_id,
            model_id=model_id,
            resolved_target_model_id=resolved_target_model_id,
            api_family=api_family,
            endpoint_id=endpoint_id,
            connection_id=connection_id,
            proxy_api_key_id=proxy_api_key_id,
            proxy_api_key_name_snapshot=proxy_api_key_name_snapshot,
            status_code=status_code,
            success_flag=success_flag,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
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
            attempt_count=attempt_count,
            request_path=request_path,
        )
        async with AsyncSessionLocal() as log_db:
            log_db.add(entry)
            try:
                await log_db.commit()
            except IntegrityError:
                await log_db.rollback()
                logger.warning(
                    "Skipped duplicate usage request event: profile_id=%d ingress_request_id=%s",
                    profile_id,
                    ingress_request_id,
                )
                return None
            await log_db.refresh(entry)
            return entry.id
    except asyncio.CancelledError:
        logger.debug("Final usage request event logging cancelled")
        return None
    except Exception:
        logger.exception("Failed to log final usage request event")
        return None


__all__ = ["log_final_usage_request_event"]
