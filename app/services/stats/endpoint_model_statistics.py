from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.models.models import Endpoint, ModelConfig, UsageRequestEvent
from app.services.stats.time_presets import resolve_time_preset

EndpointModelStatisticsPreset = Literal["all", "7h", "24h", "7d"]


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _success_rate(*, success_count: int, total_count: int) -> float:
    if total_count <= 0:
        return 0.0
    return round((success_count / total_count) * 100.0, 2)


async def get_endpoint_model_statistics(
    db: AsyncSession,
    *,
    profile_id: int,
    endpoint_id: int,
    preset: EndpointModelStatisticsPreset | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
) -> list[dict[str, object]]:
    generated_at = _normalize_datetime(to_time or utc_now())
    normalized_from_time = (
        _normalize_datetime(from_time) if from_time is not None else None
    )
    normalized_to_time = (
        _normalize_datetime(to_time) if to_time is not None else generated_at
    )
    start_at, end_at = resolve_time_preset(
        preset, normalized_from_time, normalized_to_time
    )
    normalized_start_at = (
        _normalize_datetime(start_at) if start_at is not None else None
    )
    normalized_end_at = _normalize_datetime(end_at or generated_at)

    live_endpoint_exists = await db.scalar(
        select(Endpoint.id).where(
            and_(Endpoint.profile_id == profile_id, Endpoint.id == endpoint_id)
        )
    )
    historical_usage_exists = await db.scalar(
        select(UsageRequestEvent.endpoint_id)
        .where(
            and_(
                UsageRequestEvent.profile_id == profile_id,
                UsageRequestEvent.endpoint_id == endpoint_id,
            )
        )
        .limit(1)
    )
    if live_endpoint_exists is None and historical_usage_exists is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    filters = [
        UsageRequestEvent.profile_id == profile_id,
        UsageRequestEvent.endpoint_id == endpoint_id,
        UsageRequestEvent.created_at <= normalized_end_at,
    ]
    if normalized_start_at is not None:
        filters.append(UsageRequestEvent.created_at >= normalized_start_at)

    success_count = case((UsageRequestEvent.success_flag.is_(True), 1), else_=0)
    rows = (
        await db.execute(
            select(
                UsageRequestEvent.model_id,
                ModelConfig.display_name.label("model_display_name"),
                func.count().label("request_count"),
                func.coalesce(func.sum(success_count), 0).label("success_count"),
                func.coalesce(
                    func.sum(func.coalesce(UsageRequestEvent.total_tokens, 0)), 0
                ).label("total_tokens"),
                func.coalesce(
                    func.sum(
                        func.coalesce(
                            UsageRequestEvent.total_cost_user_currency_micros,
                            0,
                        )
                    ),
                    0,
                ).label("total_cost_micros"),
            )
            .select_from(UsageRequestEvent)
            .outerjoin(
                ModelConfig,
                and_(
                    ModelConfig.profile_id == UsageRequestEvent.profile_id,
                    ModelConfig.model_id == UsageRequestEvent.model_id,
                ),
            )
            .where(and_(*filters))
            .group_by(UsageRequestEvent.model_id, ModelConfig.display_name)
        )
    ).all()

    items = [
        {
            "model_id": row.model_id,
            "model_label": row.model_display_name or row.model_id,
            "request_count": int(row.request_count or 0),
            "success_rate": _success_rate(
                success_count=int(row.success_count or 0),
                total_count=int(row.request_count or 0),
            ),
            "total_tokens": int(row.total_tokens or 0),
            "total_cost_micros": int(row.total_cost_micros or 0),
        }
        for row in rows
    ]
    items.sort(
        key=lambda row: (
            -int(row["request_count"]),
            str(row["model_label"]),
            str(row["model_id"]),
        )
    )
    return items


__all__ = ["EndpointModelStatisticsPreset", "get_endpoint_model_statistics"]
