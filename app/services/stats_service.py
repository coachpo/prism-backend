import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import String, and_, case, cast, desc, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import RequestLog, UserSetting

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
            return entry.id
    except asyncio.CancelledError:
        logger.debug("Request logging cancelled")
        return None
    except Exception:
        logger.exception("Failed to log request")
        return None


def _parse_sse_events(raw: bytes) -> list[dict]:
    events = []
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return events
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("data: ") and line != "data: [DONE]":
            try:
                events.append(json.loads(line[6:]))
            except (json.JSONDecodeError, ValueError):
                continue
    return events


def _empty_usage() -> dict[str, int | None]:
    return {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
        "reasoning_tokens": None,
    }


def _as_int(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pick_int(*values) -> int | None:
    for value in values:
        parsed = _as_int(value)
        if parsed is not None:
            return parsed
    return None


def _extract_special_usage(
    usage: dict,
) -> tuple[int | None, int | None, int | None]:
    prompt_details = (
        usage.get("prompt_tokens_details")
        or usage.get("input_tokens_details")
    )
    completion_details = (
        usage.get("completion_tokens_details")
        or usage.get("output_tokens_details")
    )

    cache_read_input_tokens = None
    cache_creation_input_tokens = None
    reasoning_tokens = None

    if isinstance(prompt_details, dict):
        cache_read_input_tokens = _pick_int(
            prompt_details.get("cached_tokens"),
            prompt_details.get("cache_read_input_tokens"),
            prompt_details.get("cached_input_tokens"),
            prompt_details.get("cachedContentTokenCount"),
        )
        cache_creation_input_tokens = _pick_int(
            prompt_details.get("cache_creation_input_tokens"),
            prompt_details.get("cache_creation_tokens"),
            prompt_details.get("cacheCreationInputTokens"),
            prompt_details.get("cacheCreationTokens"),
        )

    if isinstance(completion_details, dict):
        reasoning_tokens = _pick_int(
            completion_details.get("reasoning_tokens"),
            completion_details.get("reasoningTokenCount"),
        )

    if cache_read_input_tokens is None:
        cache_read_input_tokens = _pick_int(
            usage.get("cache_read_input_tokens"),
            usage.get("cached_input_tokens"),
            usage.get("cachedContentTokenCount"),
        )
    if cache_creation_input_tokens is None:
        cache_creation_input_tokens = _pick_int(
            usage.get("cache_creation_input_tokens"),
            usage.get("cache_creation_tokens"),
            usage.get("cacheCreationInputTokens"),
            usage.get("cacheCreationTokens"),
        )
    if reasoning_tokens is None:
        reasoning_tokens = _pick_int(
            usage.get("reasoning_tokens"),
            usage.get("reasoningTokenCount"),
        )

    return (
        cache_read_input_tokens,
        cache_creation_input_tokens,
        reasoning_tokens,
    )


def _extract_from_sse(raw: bytes) -> dict[str, int | None]:
    events = _parse_sse_events(raw)
    if not events:
        return _empty_usage()

    input_tokens = None
    output_tokens = None
    total_tokens = None
    cache_read_input_tokens = None
    cache_creation_input_tokens = None
    reasoning_tokens = None
    usage_seen = False

    for event in events:
        usage = event.get("usage")
        if usage is None:
            response_payload = event.get("response")
            if isinstance(response_payload, dict):
                nested_usage = response_payload.get("usage")
                if isinstance(nested_usage, dict):
                    usage = nested_usage

        if isinstance(usage, dict):
            usage_seen = True
            input_tokens = _pick_int(
                usage.get("prompt_tokens"),
                usage.get("input_tokens"),
                input_tokens,
            )
            output_tokens = _pick_int(
                usage.get("completion_tokens"),
                usage.get("output_tokens"),
                output_tokens,
            )
            total_tokens = _pick_int(usage.get("total_tokens"), total_tokens)
            cached_found, cache_creation_found, reasoning_found = (
                _extract_special_usage(usage)
            )
            cache_read_input_tokens = _pick_int(cached_found, cache_read_input_tokens)
            cache_creation_input_tokens = _pick_int(
                cache_creation_found,
                cache_creation_input_tokens,
            )
            reasoning_tokens = _pick_int(reasoning_found, reasoning_tokens)

        if event.get("type") == "message_start":
            msg_usage = event.get("message", {}).get("usage", {})
            if isinstance(msg_usage, dict):
                usage_seen = True
                if msg_usage.get("input_tokens") is not None:
                    input_tokens = _pick_int(
                        msg_usage.get("input_tokens"), input_tokens
                    )
                cached_found, cache_creation_found, reasoning_found = (
                    _extract_special_usage(msg_usage)
                )
                cache_read_input_tokens = _pick_int(
                    cached_found,
                    cache_read_input_tokens,
                )
                cache_creation_input_tokens = _pick_int(
                    cache_creation_found,
                    cache_creation_input_tokens,
                )
                reasoning_tokens = _pick_int(reasoning_found, reasoning_tokens)

        if event.get("type") == "message_delta":
            delta_usage = event.get("usage", {})
            if isinstance(delta_usage, dict):
                usage_seen = True
                if delta_usage.get("output_tokens") is not None:
                    output_tokens = _pick_int(
                        delta_usage.get("output_tokens"), output_tokens
                    )
                cached_found, cache_creation_found, reasoning_found = (
                    _extract_special_usage(delta_usage)
                )
                cache_read_input_tokens = _pick_int(
                    cached_found,
                    cache_read_input_tokens,
                )
                cache_creation_input_tokens = _pick_int(
                    cache_creation_found,
                    cache_creation_input_tokens,
                )
                reasoning_tokens = _pick_int(reasoning_found, reasoning_tokens)

        gemini_usage = event.get("usageMetadata")
        if gemini_usage and isinstance(gemini_usage, dict):
            usage_seen = True
            input_tokens = _pick_int(
                gemini_usage.get("promptTokenCount"),
                input_tokens,
            )
            output_tokens = _pick_int(
                gemini_usage.get("candidatesTokenCount"),
                output_tokens,
            )
            total_tokens = _pick_int(
                gemini_usage.get("totalTokenCount"),
                total_tokens,
            )
            cache_read_input_tokens = _pick_int(
                gemini_usage.get("cachedContentTokenCount"),
                cache_read_input_tokens,
            )
            reasoning_tokens = _pick_int(
                gemini_usage.get("thoughtsTokenCount"),
                reasoning_tokens,
            )

    if total_tokens is None and (input_tokens is not None or output_tokens is not None):
        total_tokens = (input_tokens or 0) + (output_tokens or 0)

    if usage_seen:
        cache_read_input_tokens = (
            cache_read_input_tokens if cache_read_input_tokens is not None else 0
        )
        cache_creation_input_tokens = (
            cache_creation_input_tokens
            if cache_creation_input_tokens is not None
            else 0
        )
        reasoning_tokens = reasoning_tokens if reasoning_tokens is not None else 0

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "reasoning_tokens": reasoning_tokens,
    }


def extract_token_usage(body: bytes | None) -> dict[str, int | None]:
    if not body:
        return _empty_usage()

    try:
        text_preview = body[:100].decode("utf-8", errors="replace")
    except Exception:
        text_preview = ""
    if "data: " in text_preview:
        return _extract_from_sse(body)

    try:
        data = json.loads(body)
        usage = data.get("usage")
        if isinstance(usage, dict):
            input_t = _pick_int(usage.get("prompt_tokens"), usage.get("input_tokens"))
            output_t = _pick_int(
                usage.get("completion_tokens"), usage.get("output_tokens")
            )
            total_t = _pick_int(usage.get("total_tokens"))
            (
                cache_read_input_tokens,
                cache_creation_input_tokens,
                reasoning_tokens,
            ) = _extract_special_usage(usage)
            cache_read_input_tokens = (
                cache_read_input_tokens if cache_read_input_tokens is not None else 0
            )
            cache_creation_input_tokens = (
                cache_creation_input_tokens
                if cache_creation_input_tokens is not None
                else 0
            )
            reasoning_tokens = reasoning_tokens if reasoning_tokens is not None else 0
            if total_t is None and (input_t is not None or output_t is not None):
                total_t = (input_t or 0) + (output_t or 0)
            return {
                "input_tokens": input_t,
                "output_tokens": output_t,
                "total_tokens": total_t,
                "cache_read_input_tokens": cache_read_input_tokens,
                "cache_creation_input_tokens": cache_creation_input_tokens,
                "reasoning_tokens": reasoning_tokens,
            }

        gemini_usage = data.get("usageMetadata")
        if gemini_usage and isinstance(gemini_usage, dict):
            input_t = _pick_int(gemini_usage.get("promptTokenCount"))
            output_t = _pick_int(gemini_usage.get("candidatesTokenCount"))
            total_t = _pick_int(gemini_usage.get("totalTokenCount"))
            cache_read_input_tokens = _pick_int(
                gemini_usage.get("cachedContentTokenCount")
            )
            reasoning_tokens = _pick_int(gemini_usage.get("thoughtsTokenCount"))
            cache_read_input_tokens = (
                cache_read_input_tokens if cache_read_input_tokens is not None else 0
            )
            cache_creation_input_tokens = 0
            reasoning_tokens = reasoning_tokens if reasoning_tokens is not None else 0
            if total_t is None and (input_t is not None or output_t is not None):
                total_t = (input_t or 0) + (output_t or 0)
            return {
                "input_tokens": input_t,
                "output_tokens": output_t,
                "total_tokens": total_t,
                "cache_read_input_tokens": cache_read_input_tokens,
                "cache_creation_input_tokens": cache_creation_input_tokens,
                "reasoning_tokens": reasoning_tokens,
            }

        if "input_tokens" in data and "usage" not in data:
            return {
                "input_tokens": _pick_int(data.get("input_tokens")),
                "output_tokens": None,
                "total_tokens": None,
                "cache_read_input_tokens": None,
                "cache_creation_input_tokens": None,
                "reasoning_tokens": None,
            }

        return _empty_usage()
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return _empty_usage()


async def get_request_logs(
    db: AsyncSession,
    *,
    profile_id: int,
    model_id: str | None = None,
    provider_type: str | None = None,
    status_code: int | None = None,
    success: bool | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[RequestLog], int]:
    filters = [RequestLog.profile_id == profile_id]
    if model_id:
        filters.append(RequestLog.model_id == model_id)
    if provider_type:
        filters.append(RequestLog.provider_type == provider_type)
    if status_code is not None:
        filters.append(RequestLog.status_code == status_code)
    if success is True:
        filters.append(RequestLog.status_code.between(200, 299))
    elif success is False:
        filters.append(~RequestLog.status_code.between(200, 299))
    if from_time:
        filters.append(RequestLog.created_at >= from_time)
    if to_time:
        filters.append(RequestLog.created_at <= to_time)
    if endpoint_id is not None:
        filters.append(RequestLog.endpoint_id == endpoint_id)
    if connection_id is not None:
        filters.append(RequestLog.connection_id == connection_id)

    where = and_(*filters) if filters else literal(True)

    count_q = select(func.count()).select_from(RequestLog).where(where)
    total = (await db.execute(count_q)).scalar() or 0

    q = (
        select(RequestLog)
        .where(where)
        .order_by(RequestLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(q)).scalars().all()
    return list(rows), total


async def get_stats_summary(
    db: AsyncSession,
    *,
    profile_id: int,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    group_by: str | None = None,
    model_id: str | None = None,
    provider_type: str | None = None,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
) -> dict:
    time_filters = [RequestLog.profile_id == profile_id]
    if from_time is not None:
        time_filters.append(RequestLog.created_at >= from_time)
    if to_time is not None:
        time_filters.append(RequestLog.created_at <= to_time)
    if model_id:
        time_filters.append(RequestLog.model_id == model_id)
    if provider_type:
        time_filters.append(RequestLog.provider_type == provider_type)
    if endpoint_id is not None:
        time_filters.append(RequestLog.endpoint_id == endpoint_id)
    if connection_id is not None:
        time_filters.append(RequestLog.connection_id == connection_id)

    time_filter = and_(*time_filters) if time_filters else literal(True)

    success_case = case(
        (RequestLog.status_code.between(200, 299), 1),
        else_=0,
    )

    agg_q = select(
        func.count().label("total_requests"),
        func.sum(success_case).label("success_count"),
        func.avg(RequestLog.response_time_ms).label("avg_response_time_ms"),
        func.coalesce(func.sum(RequestLog.input_tokens), 0).label("total_input_tokens"),
        func.coalesce(func.sum(RequestLog.output_tokens), 0).label(
            "total_output_tokens"
        ),
        func.coalesce(func.sum(RequestLog.total_tokens), 0).label("total_tokens"),
    ).where(time_filter)

    row = (await db.execute(agg_q)).one()
    total_requests = row.total_requests or 0
    success_count = row.success_count or 0
    error_count = total_requests - success_count
    success_rate = (
        round((success_count / total_requests * 100), 2) if total_requests > 0 else 0.0
    )
    avg_rt = round(row.avg_response_time_ms or 0, 1)

    p95_q = (
        select(RequestLog.response_time_ms)
        .where(time_filter)
        .order_by(RequestLog.response_time_ms.asc())
    )
    all_rts = [r for (r,) in (await db.execute(p95_q)).all()]
    p95 = 0
    if all_rts:
        idx = int(len(all_rts) * 0.95)
        idx = min(idx, len(all_rts) - 1)
        p95 = all_rts[idx]

    groups = []
    if group_by in ("model", "provider", "endpoint"):
        col_map = {
            "model": RequestLog.model_id,
            "provider": RequestLog.provider_type,
            "endpoint": RequestLog.endpoint_base_url,
        }
        group_col = col_map[group_by]
        grp_q = (
            select(
                group_col.label("key"),
                func.count().label("total_requests"),
                func.sum(success_case).label("success_count"),
                func.avg(RequestLog.response_time_ms).label("avg_response_time_ms"),
                func.coalesce(func.sum(RequestLog.total_tokens), 0).label(
                    "total_tokens"
                ),
            )
            .where(time_filter)
            .group_by(group_col)
            .order_by(func.count().desc())
        )
        grp_rows = (await db.execute(grp_q)).all()
        for g in grp_rows:
            g_total = g.total_requests or 0
            g_success = g.success_count or 0
            groups.append(
                {
                    "key": g.key or "unknown",
                    "total_requests": g_total,
                    "success_count": g_success,
                    "error_count": g_total - g_success,
                    "avg_response_time_ms": round(g.avg_response_time_ms or 0, 1),
                    "total_tokens": g.total_tokens or 0,
                }
            )

    return {
        "total_requests": total_requests,
        "success_count": success_count,
        "error_count": error_count,
        "success_rate": success_rate,
        "avg_response_time_ms": avg_rt,
        "p95_response_time_ms": p95,
        "total_input_tokens": row.total_input_tokens or 0,
        "total_output_tokens": row.total_output_tokens or 0,
        "total_tokens": row.total_tokens or 0,
        "groups": groups,
    }


async def get_connection_success_rates(
    db: AsyncSession,
    *,
    profile_id: int,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
) -> list[dict]:
    time_filters = [RequestLog.profile_id == profile_id]
    if from_time is not None:
        time_filters.append(RequestLog.created_at >= from_time)
    if to_time is not None:
        time_filters.append(RequestLog.created_at <= to_time)

    success_case = case(
        (RequestLog.status_code.between(200, 299), 1),
        else_=0,
    )

    q = (
        select(
            RequestLog.connection_id.label("connection_id"),
            func.count().label("total_requests"),
            func.sum(success_case).label("success_count"),
        )
        .where(RequestLog.connection_id.isnot(None))
        .group_by(RequestLog.connection_id)
    )
    if time_filters:
        q = q.where(and_(*time_filters))

    rows = (await db.execute(q)).all()
    results = []
    for row in rows:
        total = row.total_requests or 0
        success = row.success_count or 0
        error = total - success
        rate = round((success / total * 100), 2) if total > 0 else None
        results.append(
            {
                "connection_id": row.connection_id,
                "total_requests": total,
                "success_count": success,
                "error_count": error,
                "success_rate": rate,
            }
        )
    return results


async def get_endpoint_success_rates(
    db: AsyncSession,
    *,
    profile_id: int,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
) -> list[dict]:
    return await get_connection_success_rates(
        db,
        profile_id=profile_id,
        from_time=from_time,
        to_time=to_time,
    )


async def get_model_health_stats(
    db: AsyncSession,
    *,
    profile_id: int,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
) -> dict[str, dict]:
    time_filters = [RequestLog.profile_id == profile_id]
    if from_time is not None:
        time_filters.append(RequestLog.created_at >= from_time)
    if to_time is not None:
        time_filters.append(RequestLog.created_at <= to_time)

    success_case = case(
        (RequestLog.status_code.between(200, 299), 1),
        else_=0,
    )

    q = select(
        RequestLog.model_id.label("model_id"),
        func.count().label("total_requests"),
        func.sum(success_case).label("success_count"),
    ).group_by(RequestLog.model_id)
    if time_filters:
        q = q.where(and_(*time_filters))

    rows = (await db.execute(q)).all()
    result = {}
    for row in rows:
        total = row.total_requests or 0
        success = row.success_count or 0
        rate = round((success / total * 100), 2) if total > 0 else None
        result[row.model_id] = {
            "health_success_rate": rate,
            "health_total_requests": total,
        }
    return result


def resolve_time_preset(
    preset: str | None,
    from_time: datetime | None,
    to_time: datetime | None,
 ) -> tuple[datetime | None, datetime | None]:
    if preset in (None, "", "custom"):
        return from_time, to_time

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if preset == "today":
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return today_start, to_time
    if preset == "24h":
        return now - timedelta(days=1), to_time
    if preset in ("last_7_days", "7d"):
        return now - timedelta(days=7), to_time
    if preset in ("last_30_days", "30d"):
        return now - timedelta(days=30), to_time
    if preset == "all":
        return None, to_time
    return from_time, to_time


async def get_spending_report(
    db: AsyncSession,
    *,
    profile_id: int,
    preset: str | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    provider_type: str | None = None,
    model_id: str | None = None,
    endpoint_id: int | None = None,
    connection_id: int | None = None,
    group_by: str = "none",
    limit: int = 50,
    offset: int = 0,
    top_n: int = 5,
) -> dict:
    from_time, to_time = resolve_time_preset(preset, from_time, to_time)

    filters = [RequestLog.profile_id == profile_id]
    if from_time is not None:
        filters.append(RequestLog.created_at >= from_time)
    if to_time is not None:
        filters.append(RequestLog.created_at <= to_time)
    if provider_type:
        filters.append(RequestLog.provider_type == provider_type)
    if model_id:
        filters.append(RequestLog.model_id == model_id)
    if endpoint_id is not None:
        filters.append(RequestLog.endpoint_id == endpoint_id)
    if connection_id is not None:
        filters.append(RequestLog.connection_id == connection_id)
    where = and_(*filters) if filters else literal(True)
    success_where = and_(where, RequestLog.success_flag == True)  # noqa: E712

    spend_case = case(
        (
            RequestLog.billable_flag == True,  # noqa: E712
            func.coalesce(RequestLog.total_cost_user_currency_micros, 0),
        ),
        else_=0,
    )
    priced_case = case(
        (
            and_(
                RequestLog.success_flag == True,  # noqa: E712
                RequestLog.priced_flag == True,  # noqa: E712
            ),
            1,
        ),
        else_=0,
    )
    unpriced_case = case(
        (
            and_(
                RequestLog.success_flag == True,  # noqa: E712
                or_(
                    RequestLog.priced_flag == False,  # noqa: E712
                    RequestLog.priced_flag.is_(None),
                ),
            ),
            1,
        ),
        else_=0,
    )
    summary_row = (
        await db.execute(
            select(
                func.coalesce(func.sum(spend_case), 0).label("total_cost_micros"),
                func.count().label("successful_request_count"),
                func.coalesce(func.sum(priced_case), 0).label("priced_request_count"),
                func.coalesce(func.sum(unpriced_case), 0).label(
                    "unpriced_request_count"
                ),
                func.coalesce(
                    func.sum(func.coalesce(RequestLog.input_tokens, 0)), 0
                ).label("total_input_tokens"),
                func.coalesce(
                    func.sum(func.coalesce(RequestLog.output_tokens, 0)), 0
                ).label("total_output_tokens"),
                func.coalesce(
                    func.sum(func.coalesce(RequestLog.cache_read_input_tokens, 0)), 0
                ).label("total_cache_read_input_tokens"),
                func.coalesce(
                    func.sum(func.coalesce(RequestLog.cache_creation_input_tokens, 0)),
                    0,
                ).label("total_cache_creation_input_tokens"),
                func.coalesce(
                    func.sum(func.coalesce(RequestLog.reasoning_tokens, 0)), 0
                ).label("total_reasoning_tokens"),
                func.coalesce(
                    func.sum(func.coalesce(RequestLog.total_tokens, 0)), 0
                ).label("total_tokens"),
            ).where(success_where)
        )
    ).one()

    successful_request_count = int(summary_row.successful_request_count or 0)
    total_cost_micros = int(summary_row.total_cost_micros or 0)
    avg_cost_per_success = (
        int(total_cost_micros / successful_request_count)
        if successful_request_count > 0
        else 0
    )

    group_expr = None
    if group_by == "day":
        group_expr = func.to_char(RequestLog.created_at, "YYYY-MM-DD")
    elif group_by == "week":
        group_expr = func.to_char(func.date_trunc("week", RequestLog.created_at), 'IYYY-"W"IW')
    elif group_by == "month":
        group_expr = func.to_char(RequestLog.created_at, "YYYY-MM")
    elif group_by == "provider":
        group_expr = RequestLog.provider_type
    elif group_by == "model":
        group_expr = RequestLog.model_id
    elif group_by == "endpoint":
        group_expr = func.coalesce(
            RequestLog.endpoint_description,
            RequestLog.endpoint_base_url,
            literal("unknown_endpoint"),
        )
    elif group_by == "model_endpoint":
        group_expr = func.concat(
            RequestLog.model_id,
            literal("#"),
            func.coalesce(cast(RequestLog.endpoint_id, String), literal("-1")),
        )

    groups: list[dict] = []
    groups_total = 0
    if group_expr is not None:
        groups_total = (
            await db.execute(
                select(func.count()).select_from(
                    select(group_expr.label("group_key"))
                    .where(success_where)
                    .group_by(group_expr)
                    .subquery()
                )
            )
        ).scalar_one()

        grouped_rows = (
            await db.execute(
                select(
                    group_expr.label("key"),
                    func.count().label("total_requests"),
                    func.coalesce(func.sum(priced_case), 0).label("priced_requests"),
                    func.coalesce(func.sum(unpriced_case), 0).label(
                        "unpriced_requests"
                    ),
                    func.coalesce(
                        func.sum(func.coalesce(RequestLog.total_tokens, 0)), 0
                    ).label("total_tokens"),
                    func.coalesce(func.sum(spend_case), 0).label("total_cost_micros"),
                )
                .where(success_where)
                .group_by(group_expr)
                .order_by(desc(func.coalesce(func.sum(spend_case), 0)))
                .limit(limit)
                .offset(offset)
            )
        ).all()

        groups = [
            {
                "key": row.key or "unknown",
                "total_cost_micros": int(row.total_cost_micros or 0),
                "total_requests": int(row.total_requests or 0),
                "priced_requests": int(row.priced_requests or 0),
                "unpriced_requests": int(row.unpriced_requests or 0),
                "total_tokens": int(row.total_tokens or 0),
            }
            for row in grouped_rows
        ]
    else:
        groups = [
            {
                "key": "all",
                "total_cost_micros": total_cost_micros,
                "total_requests": successful_request_count,
                "priced_requests": int(summary_row.priced_request_count or 0),
                "unpriced_requests": int(summary_row.unpriced_request_count or 0),
                "total_tokens": int(summary_row.total_tokens or 0),
            }
        ]
        groups_total = 1

    top_model_rows = (
        await db.execute(
            select(
                RequestLog.model_id,
                func.coalesce(func.sum(spend_case), 0).label("total_cost_micros"),
            )
            .where(success_where)
            .group_by(RequestLog.model_id)
            .having(func.coalesce(func.sum(spend_case), 0) > 0)
            .order_by(desc(func.coalesce(func.sum(spend_case), 0)))
            .limit(top_n)
        )
    ).all()

    endpoint_label_expr = func.coalesce(
        RequestLog.endpoint_description,
        RequestLog.endpoint_base_url,
        literal("unknown_endpoint"),
    )
    top_endpoint_rows = (
        await db.execute(
            select(
                RequestLog.endpoint_id,
                endpoint_label_expr.label("endpoint_label"),
                func.coalesce(func.sum(spend_case), 0).label("total_cost_micros"),
            )
            .where(success_where)
            .group_by(RequestLog.endpoint_id, endpoint_label_expr)
            .having(func.coalesce(func.sum(spend_case), 0) > 0)
            .order_by(desc(func.coalesce(func.sum(spend_case), 0)))
            .limit(top_n)
        )
    ).all()

    unpriced_reason_rows = (
        await db.execute(
            select(
                RequestLog.unpriced_reason.label("reason"),
                func.count().label("reason_count"),
            )
            .where(
                success_where,
                or_(
                    RequestLog.priced_flag == False,  # noqa: E712
                    RequestLog.priced_flag.is_(None),
                ),
            )
            .group_by("reason")
            .order_by(desc(func.count()))
        )
    ).all()

    settings_row = (
        await db.execute(select(UserSetting).where(UserSetting.profile_id == profile_id).order_by(UserSetting.id.asc()).limit(1))
    ).scalar_one_or_none()

    report_currency_code = settings_row.report_currency_code if settings_row else "USD"
    report_currency_symbol = (
        settings_row.report_currency_symbol if settings_row else "$"
    )

    unpriced_breakdown: dict[str, int] = {}
    for row in unpriced_reason_rows:
        reason = row[0]
        reason_count = row[1]
        key = str(reason or "UNKNOWN")
        unpriced_breakdown[key] = unpriced_breakdown.get(key, 0) + int(
            reason_count or 0
        )

    return {
        "summary": {
            "total_cost_micros": total_cost_micros,
            "successful_request_count": successful_request_count,
            "priced_request_count": int(summary_row.priced_request_count or 0),
            "unpriced_request_count": int(summary_row.unpriced_request_count or 0),
            "total_input_tokens": int(summary_row.total_input_tokens or 0),
            "total_output_tokens": int(summary_row.total_output_tokens or 0),
            "total_cache_read_input_tokens": int(
                summary_row.total_cache_read_input_tokens or 0
            ),
            "total_cache_creation_input_tokens": int(
                summary_row.total_cache_creation_input_tokens or 0
            ),
            "total_reasoning_tokens": int(summary_row.total_reasoning_tokens or 0),
            "total_tokens": int(summary_row.total_tokens or 0),
            "avg_cost_per_successful_request_micros": avg_cost_per_success,
        },
        "groups": groups,
        "groups_total": int(groups_total or 0),
        "top_spending_models": [
            {
                "model_id": row.model_id,
                "total_cost_micros": int(row.total_cost_micros or 0),
            }
            for row in top_model_rows
        ],
        "top_spending_endpoints": [
            {
                "endpoint_id": row.endpoint_id,
                "endpoint_label": row.endpoint_label,
                "total_cost_micros": int(row.total_cost_micros or 0),
            }
            for row in top_endpoint_rows
        ],
        "unpriced_breakdown": unpriced_breakdown,
        "report_currency_code": report_currency_code,
        "report_currency_symbol": report_currency_symbol,
    }
