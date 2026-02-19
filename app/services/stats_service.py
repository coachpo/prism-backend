import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select, func, case, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import RequestLog

logger = logging.getLogger(__name__)


async def log_request(
    db: AsyncSession,
    *,
    model_id: str,
    provider_type: str,
    endpoint_id: int | None,
    endpoint_base_url: str | None,
    status_code: int,
    response_time_ms: int,
    is_stream: bool,
    request_path: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    error_detail: str | None = None,
) -> None:
    try:
        entry = RequestLog(
            model_id=model_id,
            provider_type=provider_type,
            endpoint_id=endpoint_id,
            endpoint_base_url=endpoint_base_url,
            status_code=status_code,
            response_time_ms=response_time_ms,
            is_stream=is_stream,
            request_path=request_path,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            error_detail=error_detail,
        )
        db.add(entry)
        await db.flush()
    except Exception:
        logger.exception("Failed to log request")


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


def _extract_from_sse(raw: bytes) -> dict[str, int | None]:
    events = _parse_sse_events(raw)
    if not events:
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}

    input_tokens = None
    output_tokens = None
    total_tokens = None

    for event in events:
        # OpenAI: final chunk with usage object
        usage = event.get("usage")
        if usage and isinstance(usage, dict):
            input_tokens = (
                usage.get("prompt_tokens") or usage.get("input_tokens") or input_tokens
            )
            output_tokens = (
                usage.get("completion_tokens")
                or usage.get("output_tokens")
                or output_tokens
            )
            total_tokens = usage.get("total_tokens") or total_tokens

        # Anthropic: message_start → message.usage.input_tokens
        if event.get("type") == "message_start":
            msg_usage = event.get("message", {}).get("usage", {})
            if msg_usage.get("input_tokens") is not None:
                input_tokens = msg_usage["input_tokens"]

        # Anthropic: message_delta → usage.output_tokens
        if event.get("type") == "message_delta":
            delta_usage = event.get("usage", {})
            if delta_usage.get("output_tokens") is not None:
                output_tokens = delta_usage["output_tokens"]

    if total_tokens is None and (input_tokens is not None or output_tokens is not None):
        total_tokens = (input_tokens or 0) + (output_tokens or 0)

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def extract_token_usage(body: bytes | None) -> dict[str, int | None]:
    if not body:
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}

    try:
        text_preview = body[:100].decode("utf-8", errors="replace")
    except Exception:
        text_preview = ""
    if "data: " in text_preview:
        return _extract_from_sse(body)

    try:
        data = json.loads(body)
        usage = data.get("usage", {})
        if usage:
            input_t = usage.get("prompt_tokens") or usage.get("input_tokens")
            output_t = usage.get("completion_tokens") or usage.get("output_tokens")
            total_t = usage.get("total_tokens")
            if total_t is None and (input_t is not None or output_t is not None):
                total_t = (input_t or 0) + (output_t or 0)
            return {
                "input_tokens": input_t,
                "output_tokens": output_t,
                "total_tokens": total_t,
            }

        # Anthropic count_tokens: top-level input_tokens without usage wrapper
        if "input_tokens" in data and "usage" not in data:
            return {
                "input_tokens": data["input_tokens"],
                "output_tokens": None,
                "total_tokens": None,
            }

        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}


async def get_request_logs(
    db: AsyncSession,
    *,
    model_id: str | None = None,
    provider_type: str | None = None,
    status_code: int | None = None,
    success: bool | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[RequestLog], int]:
    filters = []
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

    where = and_(*filters) if filters else True

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
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    group_by: str | None = None,
) -> dict:
    if from_time is None:
        from_time = datetime.utcnow() - timedelta(hours=24)
    if to_time is None:
        to_time = datetime.utcnow()

    time_filter = and_(
        RequestLog.created_at >= from_time,
        RequestLog.created_at <= to_time,
    )

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


async def get_endpoint_success_rates(
    db: AsyncSession,
    *,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
) -> list[dict]:
    if from_time is None:
        from_time = datetime.utcnow() - timedelta(hours=24)
    if to_time is None:
        to_time = datetime.utcnow()

    time_filter = and_(
        RequestLog.created_at >= from_time,
        RequestLog.created_at <= to_time,
    )

    success_case = case(
        (RequestLog.status_code.between(200, 299), 1),
        else_=0,
    )

    q = (
        select(
            RequestLog.endpoint_id.label("endpoint_id"),
            func.count().label("total_requests"),
            func.sum(success_case).label("success_count"),
        )
        .where(time_filter)
        .where(RequestLog.endpoint_id.isnot(None))
        .group_by(RequestLog.endpoint_id)
    )

    rows = (await db.execute(q)).all()
    results = []
    for row in rows:
        total = row.total_requests or 0
        success = row.success_count or 0
        error = total - success
        rate = round((success / total * 100), 2) if total > 0 else None
        results.append(
            {
                "endpoint_id": row.endpoint_id,
                "total_requests": total,
                "success_count": success,
                "error_count": error,
                "success_rate": rate,
            }
        )
    return results


async def get_model_health_stats(
    db: AsyncSession,
    *,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
) -> dict[str, dict]:
    if from_time is None:
        from_time = datetime.utcnow() - timedelta(hours=24)
    if to_time is None:
        to_time = datetime.utcnow()

    time_filter = and_(
        RequestLog.created_at >= from_time,
        RequestLog.created_at <= to_time,
    )

    success_case = case(
        (RequestLog.status_code.between(200, 299), 1),
        else_=0,
    )

    q = (
        select(
            RequestLog.model_id.label("model_id"),
            func.count().label("total_requests"),
            func.sum(success_case).label("success_count"),
        )
        .where(time_filter)
        .group_by(RequestLog.model_id)
    )

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
