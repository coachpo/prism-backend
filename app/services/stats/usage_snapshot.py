from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal, cast

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.models.models import Endpoint, ModelConfig, ProxyApiKey, UsageRequestEvent
from app.services.stats.time_presets import resolve_time_preset
from app.services.user_settings import get_report_currency_preferences

UsageSnapshotPreset = Literal["all", "7h", "24h", "7d"]

ROLLING_WINDOW_MINUTES = 30
REQUEST_EVENT_RENDER_LIMIT = 500
SERVICE_HEALTH_DAYS = 7
SERVICE_HEALTH_INTERVAL_MINUTES = 15


@dataclass(slots=True)
class _SnapshotEvent:
    api_family: str
    attempt_count: int
    cached_tokens: int
    connection_id: int | None
    created_at: datetime
    endpoint_id: int | None
    endpoint_label: str
    has_pricing_data: bool
    ingress_request_id: str
    input_tokens: int
    model_id: str
    model_label: str
    output_tokens: int
    proxy_api_key_id: int | None
    proxy_api_key_label: str | None
    proxy_api_key_stats_label: str
    proxy_api_key_prefix: str | None
    reasoning_tokens: int
    request_path: str
    resolved_target_model_id: str | None
    status_code: int
    success_flag: bool
    total_cost_micros: int
    total_tokens: int


@dataclass(slots=True)
class _EndpointModelAggregate:
    model_id: str
    model_label: str
    request_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    total_tokens: int = 0
    total_cost_micros: int = 0


@dataclass(slots=True)
class _EndpointAggregate:
    endpoint_id: int | None
    endpoint_label: str
    request_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    total_tokens: int = 0
    total_cost_micros: int = 0
    models: dict[str, _EndpointModelAggregate] = field(default_factory=dict)


@dataclass(slots=True)
class _ModelAggregate:
    model_id: str
    model_label: str
    api_family: str
    request_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    total_tokens: int = 0
    total_cost_micros: int = 0


@dataclass(slots=True)
class _ProxyKeyAggregate:
    proxy_api_key_id: int | None
    proxy_api_key_label: str
    key_prefix: str | None
    request_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    total_tokens: int = 0
    total_cost_micros: int = 0


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _coalesce_int(value: int | None) -> int:
    return int(value or 0)


def _success_rate(*, success_count: int, total_count: int) -> float:
    if total_count <= 0:
        return 0.0
    return round((success_count / total_count) * 100.0, 2)


def _label_sort_key(value: str) -> tuple[str, str]:
    return (value.casefold(), value)


def _bucket_floor(value: datetime, granularity: Literal["hour", "day"]) -> datetime:
    normalized = _normalize_datetime(value)
    if granularity == "hour":
        return normalized.replace(minute=0, second=0, microsecond=0)
    return normalized.replace(hour=0, minute=0, second=0, microsecond=0)


def _bucket_step(granularity: Literal["hour", "day"]) -> timedelta:
    if granularity == "hour":
        return timedelta(hours=1)
    return timedelta(days=1)


def _service_health_bucket_floor(value: datetime) -> datetime:
    normalized = _normalize_datetime(value)
    minute = (
        normalized.minute // SERVICE_HEALTH_INTERVAL_MINUTES
    ) * SERVICE_HEALTH_INTERVAL_MINUTES
    return normalized.replace(minute=minute, second=0, microsecond=0)


def _bucket_minutes(granularity: Literal["hour", "day"]) -> float:
    if granularity == "hour":
        return 60.0
    return 1440.0


def _bucket_range(
    *,
    start_at: datetime | None,
    end_at: datetime,
    events: list[_SnapshotEvent],
    granularity: Literal["hour", "day"],
) -> list[datetime]:
    if start_at is None:
        if events:
            current = _bucket_floor(
                min(event.created_at for event in events), granularity
            )
        else:
            current = _bucket_floor(end_at, granularity)
    else:
        current = _bucket_floor(start_at, granularity)

    end_bucket = _bucket_floor(end_at, granularity)
    step = _bucket_step(granularity)
    buckets: list[datetime] = []

    while current <= end_bucket:
        buckets.append(current)
        current += step

    return buckets


def _effective_window_start(
    *,
    start_at: datetime | None,
    end_at: datetime,
    events: list[_SnapshotEvent],
) -> datetime:
    if start_at is not None:
        return start_at
    if events:
        return min(event.created_at for event in events)
    return end_at


def _service_health_status(
    *, request_count: int, success_count: int, failed_count: int
) -> Literal["ok", "degraded", "down", "empty"]:
    if request_count <= 0:
        return "empty"
    if failed_count <= 0:
        return "ok"
    if success_count <= 0:
        return "down"
    return "degraded"


def _build_service_health(
    *,
    events: list[_SnapshotEvent],
    end_at: datetime,
) -> dict[str, object]:
    service_health_start = _bucket_floor(end_at, "day") - timedelta(
        days=SERVICE_HEALTH_DAYS - 1
    )
    service_health_end = service_health_start + timedelta(days=SERVICE_HEALTH_DAYS)
    daily_stats = defaultdict(
        lambda: {"request_count": 0, "success_count": 0, "failed_count": 0}
    )
    cell_stats = defaultdict(
        lambda: {"request_count": 0, "success_count": 0, "failed_count": 0}
    )

    for event in events:
        day_bucket = _bucket_floor(event.created_at, "day")
        if day_bucket < service_health_start or day_bucket >= service_health_end:
            continue

        daily_stats[day_bucket]["request_count"] += 1
        cell_bucket = _service_health_bucket_floor(event.created_at)
        cell_stats[cell_bucket]["request_count"] += 1

        if event.success_flag:
            daily_stats[day_bucket]["success_count"] += 1
            cell_stats[cell_bucket]["success_count"] += 1
        else:
            daily_stats[day_bucket]["failed_count"] += 1
            cell_stats[cell_bucket]["failed_count"] += 1

    daily: list[dict[str, object]] = []
    for offset in range(SERVICE_HEALTH_DAYS):
        bucket = service_health_start + timedelta(days=offset)
        request_count = daily_stats[bucket]["request_count"]
        success_count = daily_stats[bucket]["success_count"]
        failed_count = daily_stats[bucket]["failed_count"]
        daily.append(
            {
                "bucket_start": bucket,
                "request_count": request_count,
                "success_count": success_count,
                "failed_count": failed_count,
                "availability_percentage": (
                    _success_rate(
                        success_count=success_count,
                        total_count=request_count,
                    )
                    if request_count > 0
                    else None
                ),
            }
        )

    cells: list[dict[str, object]] = []
    bucket = service_health_start
    cell_count = SERVICE_HEALTH_DAYS * (24 * 60 // SERVICE_HEALTH_INTERVAL_MINUTES)
    for _ in range(cell_count):
        request_count = cell_stats[bucket]["request_count"]
        success_count = cell_stats[bucket]["success_count"]
        failed_count = cell_stats[bucket]["failed_count"]
        cells.append(
            {
                "bucket_start": bucket,
                "request_count": request_count,
                "success_count": success_count,
                "failed_count": failed_count,
                "availability_percentage": (
                    _success_rate(
                        success_count=success_count,
                        total_count=request_count,
                    )
                    if request_count > 0
                    else None
                ),
                "status": _service_health_status(
                    request_count=request_count,
                    success_count=success_count,
                    failed_count=failed_count,
                ),
            }
        )
        bucket += timedelta(minutes=SERVICE_HEALTH_INTERVAL_MINUTES)

    return {
        "days": SERVICE_HEALTH_DAYS,
        "interval_minutes": SERVICE_HEALTH_INTERVAL_MINUTES,
        "daily": daily,
        "cells": cells,
    }


def _build_request_event_available_filters(
    events: list[_SnapshotEvent],
) -> dict[str, object]:
    models: dict[str, dict[str, str]] = {}
    endpoints: dict[tuple[int | None, str], dict[str, object]] = {}
    api_families: dict[str, dict[str, str]] = {}
    proxy_api_keys: dict[tuple[int, str, str | None], dict[str, object]] = {}

    for event in events:
        models[event.model_id] = {
            "model_id": event.model_id,
            "label": event.model_label,
        }
        endpoints[(event.endpoint_id, event.endpoint_label)] = {
            "endpoint_id": event.endpoint_id,
            "label": event.endpoint_label,
        }
        api_families[event.api_family] = {
            "api_family": event.api_family,
            "label": event.api_family,
        }
        if event.proxy_api_key_id is not None:
            proxy_api_keys[
                (
                    event.proxy_api_key_id,
                    event.proxy_api_key_stats_label,
                    event.proxy_api_key_prefix,
                )
            ] = {
                "proxy_api_key_id": event.proxy_api_key_id,
                "label": event.proxy_api_key_stats_label,
                "key_prefix": event.proxy_api_key_prefix,
            }

    return {
        "models": sorted(
            models.values(),
            key=lambda row: (
                _label_sort_key(cast(str, row["label"])),
                cast(str, row["model_id"]),
            ),
        ),
        "endpoints": sorted(
            endpoints.values(),
            key=lambda row: (
                _label_sort_key(cast(str, row["label"])),
                cast(int | None, row["endpoint_id"]) or -1,
            ),
        ),
        "api_families": sorted(
            api_families.values(),
            key=lambda row: _label_sort_key(cast(str, row["label"])),
        ),
        "proxy_api_keys": sorted(
            proxy_api_keys.values(),
            key=lambda row: (
                _label_sort_key(cast(str, row["label"])),
                cast(int | None, row["proxy_api_key_id"]) or -1,
            ),
        ),
    }


def _event_request_row(event: _SnapshotEvent) -> dict[str, object]:
    return {
        "ingress_request_id": event.ingress_request_id,
        "created_at": event.created_at,
        "model_id": event.model_id,
        "model_label": event.model_label,
        "resolved_target_model_id": event.resolved_target_model_id,
        "api_family": event.api_family,
        "endpoint_id": event.endpoint_id,
        "endpoint_label": event.endpoint_label,
        "connection_id": event.connection_id,
        "status_code": event.status_code,
        "success_flag": event.success_flag,
        "attempt_count": event.attempt_count,
        "request_path": event.request_path,
        "input_tokens": event.input_tokens,
        "output_tokens": event.output_tokens,
        "cached_tokens": event.cached_tokens,
        "reasoning_tokens": event.reasoning_tokens,
        "total_tokens": event.total_tokens,
        "total_cost_micros": event.total_cost_micros,
        "proxy_api_key": {
            "label": event.proxy_api_key_label,
            "key_prefix": event.proxy_api_key_prefix,
        },
    }


def _build_request_trend_series(
    *,
    events: list[_SnapshotEvent],
    start_at: datetime | None,
    end_at: datetime,
    granularity: Literal["hour", "day"],
) -> list[dict[str, object]]:
    buckets = _bucket_range(
        start_at=start_at,
        end_at=end_at,
        events=events,
        granularity=granularity,
    )
    bucket_minutes = _bucket_minutes(granularity)
    overall = defaultdict(
        lambda: {"request_count": 0, "success_count": 0, "failed_count": 0}
    )
    model_totals: dict[str, int] = defaultdict(int)
    model_labels: dict[str, str] = {}
    by_model: dict[str, dict[datetime, dict[str, int]]] = defaultdict(
        lambda: defaultdict(
            lambda: {"request_count": 0, "success_count": 0, "failed_count": 0}
        )
    )

    for event in events:
        bucket = _bucket_floor(event.created_at, granularity)
        overall[bucket]["request_count"] += 1
        if event.success_flag:
            overall[bucket]["success_count"] += 1
        else:
            overall[bucket]["failed_count"] += 1

        model_totals[event.model_id] += 1
        model_labels[event.model_id] = event.model_label
        by_model[event.model_id][bucket]["request_count"] += 1
        if event.success_flag:
            by_model[event.model_id][bucket]["success_count"] += 1
        else:
            by_model[event.model_id][bucket]["failed_count"] += 1

    series: list[dict[str, object]] = [
        {
            "key": "all",
            "label": "All Models",
            "total_requests": len(events),
            "points": [
                {
                    "bucket_start": bucket,
                    "request_count": overall[bucket]["request_count"],
                    "success_count": overall[bucket]["success_count"],
                    "failed_count": overall[bucket]["failed_count"],
                    "rpm": round(overall[bucket]["request_count"] / bucket_minutes, 3),
                }
                for bucket in buckets
            ],
        }
    ]

    for model_id in sorted(
        model_totals, key=lambda value: (model_labels[value], value)
    ):
        series.append(
            {
                "key": model_id,
                "label": model_labels[model_id],
                "total_requests": model_totals[model_id],
                "points": [
                    {
                        "bucket_start": bucket,
                        "request_count": by_model[model_id][bucket]["request_count"],
                        "success_count": by_model[model_id][bucket]["success_count"],
                        "failed_count": by_model[model_id][bucket]["failed_count"],
                        "rpm": round(
                            by_model[model_id][bucket]["request_count"]
                            / bucket_minutes,
                            3,
                        ),
                    }
                    for bucket in buckets
                ],
            }
        )

    return series


def _build_token_trend_series(
    *,
    events: list[_SnapshotEvent],
    start_at: datetime | None,
    end_at: datetime,
    granularity: Literal["hour", "day"],
) -> list[dict[str, object]]:
    buckets = _bucket_range(
        start_at=start_at,
        end_at=end_at,
        events=events,
        granularity=granularity,
    )
    bucket_minutes = _bucket_minutes(granularity)
    zero_stats = {
        "total_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
    }
    overall = defaultdict(lambda: dict(zero_stats))
    model_totals: dict[str, int] = defaultdict(int)
    model_labels: dict[str, str] = {}
    by_model: dict[str, dict[datetime, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: dict(zero_stats))
    )

    for event in events:
        bucket = _bucket_floor(event.created_at, granularity)
        overall[bucket]["total_tokens"] += event.total_tokens
        overall[bucket]["input_tokens"] += event.input_tokens
        overall[bucket]["output_tokens"] += event.output_tokens
        overall[bucket]["cached_tokens"] += event.cached_tokens
        overall[bucket]["reasoning_tokens"] += event.reasoning_tokens

        model_totals[event.model_id] += event.total_tokens
        model_labels[event.model_id] = event.model_label
        by_model[event.model_id][bucket]["total_tokens"] += event.total_tokens
        by_model[event.model_id][bucket]["input_tokens"] += event.input_tokens
        by_model[event.model_id][bucket]["output_tokens"] += event.output_tokens
        by_model[event.model_id][bucket]["cached_tokens"] += event.cached_tokens
        by_model[event.model_id][bucket]["reasoning_tokens"] += event.reasoning_tokens

    series: list[dict[str, object]] = [
        {
            "key": "all",
            "label": "All Models",
            "total_tokens": sum(event.total_tokens for event in events),
            "points": [
                {
                    "bucket_start": bucket,
                    "total_tokens": overall[bucket]["total_tokens"],
                    "input_tokens": overall[bucket]["input_tokens"],
                    "output_tokens": overall[bucket]["output_tokens"],
                    "cached_tokens": overall[bucket]["cached_tokens"],
                    "reasoning_tokens": overall[bucket]["reasoning_tokens"],
                    "tpm": round(overall[bucket]["total_tokens"] / bucket_minutes, 3),
                }
                for bucket in buckets
            ],
        }
    ]

    for model_id in sorted(
        model_totals, key=lambda value: (model_labels[value], value)
    ):
        series.append(
            {
                "key": model_id,
                "label": model_labels[model_id],
                "total_tokens": model_totals[model_id],
                "points": [
                    {
                        "bucket_start": bucket,
                        "total_tokens": by_model[model_id][bucket]["total_tokens"],
                        "input_tokens": by_model[model_id][bucket]["input_tokens"],
                        "output_tokens": by_model[model_id][bucket]["output_tokens"],
                        "cached_tokens": by_model[model_id][bucket]["cached_tokens"],
                        "reasoning_tokens": by_model[model_id][bucket][
                            "reasoning_tokens"
                        ],
                        "tpm": round(
                            by_model[model_id][bucket]["total_tokens"] / bucket_minutes,
                            3,
                        ),
                    }
                    for bucket in buckets
                ],
            }
        )

    return series


def _build_token_type_breakdown(
    *,
    events: list[_SnapshotEvent],
    start_at: datetime | None,
    end_at: datetime,
    granularity: Literal["hour", "day"],
) -> list[dict[str, object]]:
    buckets = _bucket_range(
        start_at=start_at,
        end_at=end_at,
        events=events,
        granularity=granularity,
    )
    stats_by_bucket = defaultdict(
        lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
        }
    )

    for event in events:
        bucket = _bucket_floor(event.created_at, granularity)
        stats_by_bucket[bucket]["input_tokens"] += event.input_tokens
        stats_by_bucket[bucket]["output_tokens"] += event.output_tokens
        stats_by_bucket[bucket]["cached_tokens"] += event.cached_tokens
        stats_by_bucket[bucket]["reasoning_tokens"] += event.reasoning_tokens

    return [
        {
            "bucket_start": bucket,
            "input_tokens": stats_by_bucket[bucket]["input_tokens"],
            "output_tokens": stats_by_bucket[bucket]["output_tokens"],
            "cached_tokens": stats_by_bucket[bucket]["cached_tokens"],
            "reasoning_tokens": stats_by_bucket[bucket]["reasoning_tokens"],
        }
        for bucket in buckets
    ]


def _build_cost_overview(
    *,
    events: list[_SnapshotEvent],
    start_at: datetime | None,
    end_at: datetime,
) -> dict[str, object]:
    priced_request_count = sum(
        1 for event in events if event.success_flag and event.has_pricing_data
    )
    unpriced_request_count = sum(
        1 for event in events if event.success_flag and not event.has_pricing_data
    )

    def _points(granularity: Literal["hour", "day"]) -> list[dict[str, object]]:
        buckets = _bucket_range(
            start_at=start_at,
            end_at=end_at,
            events=events,
            granularity=granularity,
        )
        totals = defaultdict(int)
        for event in events:
            totals[_bucket_floor(event.created_at, granularity)] += (
                event.total_cost_micros
            )
        return [
            {"bucket_start": bucket, "total_cost_micros": totals[bucket]}
            for bucket in buckets
        ]

    return {
        "total_cost_micros": sum(event.total_cost_micros for event in events),
        "priced_request_count": priced_request_count,
        "unpriced_request_count": unpriced_request_count,
        "hourly": _points("hour"),
        "daily": _points("day"),
    }


def _build_endpoint_statistics(events: list[_SnapshotEvent]) -> list[dict[str, object]]:
    endpoint_groups: dict[tuple[int | None, str], _EndpointAggregate] = {}

    for event in events:
        key = (event.endpoint_id, event.endpoint_label)
        group = endpoint_groups.setdefault(
            key,
            _EndpointAggregate(
                endpoint_id=event.endpoint_id,
                endpoint_label=event.endpoint_label,
            ),
        )
        group.request_count += 1
        group.success_count += int(event.success_flag)
        group.failed_count += int(not event.success_flag)
        group.total_tokens += event.total_tokens
        group.total_cost_micros += event.total_cost_micros

        model_group = group.models.setdefault(
            event.model_id,
            _EndpointModelAggregate(
                model_id=event.model_id,
                model_label=event.model_label,
            ),
        )
        model_group.request_count += 1
        model_group.success_count += int(event.success_flag)
        model_group.failed_count += int(not event.success_flag)
        model_group.total_tokens += event.total_tokens
        model_group.total_cost_micros += event.total_cost_micros

    rows: list[dict[str, object]] = []
    for group in endpoint_groups.values():
        model_rows: list[dict[str, object]] = []
        for model_row in group.models.values():
            model_rows.append(
                {
                    "model_id": model_row.model_id,
                    "model_label": model_row.model_label,
                    "request_count": model_row.request_count,
                    "success_count": model_row.success_count,
                    "failed_count": model_row.failed_count,
                    "success_rate": _success_rate(
                        success_count=model_row.success_count,
                        total_count=model_row.request_count,
                    ),
                    "total_tokens": model_row.total_tokens,
                    "total_cost_micros": model_row.total_cost_micros,
                }
            )
        model_rows.sort(
            key=lambda row: (
                -cast(int, row["request_count"]),
                cast(str, row["model_label"]),
            )
        )

        rows.append(
            {
                "endpoint_id": group.endpoint_id,
                "endpoint_label": group.endpoint_label,
                "request_count": group.request_count,
                "success_count": group.success_count,
                "failed_count": group.failed_count,
                "success_rate": _success_rate(
                    success_count=group.success_count,
                    total_count=group.request_count,
                ),
                "total_tokens": group.total_tokens,
                "total_cost_micros": group.total_cost_micros,
                "models": model_rows,
            }
        )

    rows.sort(
        key=lambda row: (
            -cast(int, row["request_count"]),
            cast(str, row["endpoint_label"]),
        )
    )
    return rows


def _build_model_statistics(events: list[_SnapshotEvent]) -> list[dict[str, object]]:
    model_groups: dict[str, _ModelAggregate] = {}

    for event in events:
        group = model_groups.setdefault(
            event.model_id,
            _ModelAggregate(
                model_id=event.model_id,
                model_label=event.model_label,
                api_family=event.api_family,
            ),
        )
        group.request_count += 1
        group.success_count += int(event.success_flag)
        group.failed_count += int(not event.success_flag)
        group.total_tokens += event.total_tokens
        group.total_cost_micros += event.total_cost_micros

    rows: list[dict[str, object]] = []
    for row in model_groups.values():
        rows.append(
            {
                "model_id": row.model_id,
                "model_label": row.model_label,
                "api_family": row.api_family,
                "request_count": row.request_count,
                "success_count": row.success_count,
                "failed_count": row.failed_count,
                "success_rate": _success_rate(
                    success_count=row.success_count,
                    total_count=row.request_count,
                ),
                "total_tokens": row.total_tokens,
                "total_cost_micros": row.total_cost_micros,
            }
        )
    rows.sort(
        key=lambda row: (
            -cast(int, row["request_count"]),
            cast(str, row["model_label"]),
        )
    )
    return rows


def _build_proxy_api_key_statistics(
    events: list[_SnapshotEvent],
) -> list[dict[str, object]]:
    groups: dict[tuple[int | None, str, str | None], _ProxyKeyAggregate] = {}

    for event in events:
        key = (
            event.proxy_api_key_id,
            event.proxy_api_key_stats_label,
            event.proxy_api_key_prefix,
        )
        group = groups.setdefault(
            key,
            _ProxyKeyAggregate(
                proxy_api_key_id=event.proxy_api_key_id,
                proxy_api_key_label=event.proxy_api_key_stats_label,
                key_prefix=event.proxy_api_key_prefix,
            ),
        )
        group.request_count += 1
        group.success_count += int(event.success_flag)
        group.failed_count += int(not event.success_flag)
        group.total_tokens += event.total_tokens
        group.total_cost_micros += event.total_cost_micros

    rows: list[dict[str, object]] = []
    for row in groups.values():
        rows.append(
            {
                "proxy_api_key_id": row.proxy_api_key_id,
                "proxy_api_key_label": row.proxy_api_key_label,
                "key_prefix": row.key_prefix,
                "request_count": row.request_count,
                "success_count": row.success_count,
                "failed_count": row.failed_count,
                "success_rate": _success_rate(
                    success_count=row.success_count,
                    total_count=row.request_count,
                ),
                "total_tokens": row.total_tokens,
                "total_cost_micros": row.total_cost_micros,
            }
        )
    rows.sort(
        key=lambda row: (
            -cast(int, row["request_count"]),
            cast(str, row["proxy_api_key_label"]),
        )
    )
    return rows


async def _load_snapshot_events(
    db: AsyncSession,
    *,
    profile_id: int,
    start_at: datetime | None,
    end_at: datetime,
) -> list[_SnapshotEvent]:
    filters = [
        UsageRequestEvent.profile_id == profile_id,
        UsageRequestEvent.created_at <= end_at,
    ]
    if start_at is not None:
        filters.append(UsageRequestEvent.created_at >= start_at)

    rows = (
        await db.execute(
            select(
                UsageRequestEvent.api_family,
                UsageRequestEvent.attempt_count,
                UsageRequestEvent.cache_creation_input_tokens,
                UsageRequestEvent.cache_read_input_tokens,
                UsageRequestEvent.connection_id,
                UsageRequestEvent.created_at,
                UsageRequestEvent.endpoint_id,
                UsageRequestEvent.ingress_request_id,
                UsageRequestEvent.input_tokens,
                UsageRequestEvent.model_id,
                UsageRequestEvent.output_tokens,
                UsageRequestEvent.proxy_api_key_id,
                UsageRequestEvent.proxy_api_key_name_snapshot,
                UsageRequestEvent.reasoning_tokens,
                UsageRequestEvent.request_path,
                UsageRequestEvent.resolved_target_model_id,
                UsageRequestEvent.status_code,
                UsageRequestEvent.success_flag,
                UsageRequestEvent.total_cost_user_currency_micros,
                UsageRequestEvent.total_tokens,
                ModelConfig.display_name.label("model_display_name"),
                Endpoint.name.label("endpoint_name"),
                Endpoint.base_url.label("endpoint_base_url"),
                ProxyApiKey.name.label("current_proxy_api_key_name"),
                ProxyApiKey.key_prefix.label("current_proxy_api_key_prefix"),
            )
            .select_from(UsageRequestEvent)
            .outerjoin(
                ModelConfig,
                and_(
                    ModelConfig.profile_id == UsageRequestEvent.profile_id,
                    ModelConfig.model_id == UsageRequestEvent.model_id,
                ),
            )
            .outerjoin(
                Endpoint,
                and_(
                    Endpoint.profile_id == UsageRequestEvent.profile_id,
                    Endpoint.id == UsageRequestEvent.endpoint_id,
                ),
            )
            .outerjoin(
                ProxyApiKey,
                ProxyApiKey.id == UsageRequestEvent.proxy_api_key_id,
            )
            .where(and_(*filters))
            .order_by(UsageRequestEvent.created_at.desc(), UsageRequestEvent.id.desc())
        )
    ).all()

    events: list[_SnapshotEvent] = []
    for row in rows:
        endpoint_label = row.endpoint_name or row.endpoint_base_url
        if endpoint_label is None and row.endpoint_id is not None:
            endpoint_label = f"Endpoint {row.endpoint_id}"
        if endpoint_label is None:
            endpoint_label = "Unknown Endpoint"

        proxy_api_key_label = (
            row.proxy_api_key_name_snapshot or row.current_proxy_api_key_name
        )
        proxy_api_key_stats_label = proxy_api_key_label or "Unknown Proxy API Key"

        events.append(
            _SnapshotEvent(
                api_family=row.api_family,
                attempt_count=int(row.attempt_count),
                cached_tokens=_coalesce_int(row.cache_read_input_tokens)
                + _coalesce_int(row.cache_creation_input_tokens),
                connection_id=row.connection_id,
                created_at=_normalize_datetime(row.created_at),
                endpoint_id=row.endpoint_id,
                endpoint_label=endpoint_label,
                has_pricing_data=row.total_cost_user_currency_micros is not None,
                ingress_request_id=row.ingress_request_id,
                input_tokens=_coalesce_int(row.input_tokens),
                model_id=row.model_id,
                model_label=row.model_display_name or row.model_id,
                output_tokens=_coalesce_int(row.output_tokens),
                proxy_api_key_id=row.proxy_api_key_id,
                proxy_api_key_label=proxy_api_key_label,
                proxy_api_key_stats_label=proxy_api_key_stats_label,
                proxy_api_key_prefix=row.current_proxy_api_key_prefix,
                reasoning_tokens=_coalesce_int(row.reasoning_tokens),
                request_path=row.request_path,
                resolved_target_model_id=row.resolved_target_model_id,
                status_code=int(row.status_code),
                success_flag=bool(row.success_flag),
                total_cost_micros=_coalesce_int(row.total_cost_user_currency_micros),
                total_tokens=_coalesce_int(row.total_tokens),
            )
        )

    return events


async def get_usage_snapshot(
    db: AsyncSession,
    *,
    profile_id: int,
    preset: UsageSnapshotPreset = "24h",
) -> dict[str, object]:
    generated_at = _normalize_datetime(utc_now())
    start_at, end_at = resolve_time_preset(preset, None, generated_at)
    normalized_start_at = (
        _normalize_datetime(start_at) if start_at is not None else None
    )
    normalized_end_at = _normalize_datetime(end_at or generated_at)

    events = await _load_snapshot_events(
        db,
        profile_id=profile_id,
        start_at=normalized_start_at,
        end_at=normalized_end_at,
    )
    currency_code, currency_symbol = await get_report_currency_preferences(
        db,
        profile_id=profile_id,
    )

    total_requests = len(events)
    success_requests = sum(1 for event in events if event.success_flag)
    failed_requests = total_requests - success_requests
    total_tokens = sum(event.total_tokens for event in events)
    input_tokens = sum(event.input_tokens for event in events)
    output_tokens = sum(event.output_tokens for event in events)
    cached_tokens = sum(event.cached_tokens for event in events)
    reasoning_tokens = sum(event.reasoning_tokens for event in events)
    effective_window_start = _effective_window_start(
        start_at=normalized_start_at,
        end_at=normalized_end_at,
        events=events,
    )
    window_minutes = max(
        (normalized_end_at - effective_window_start).total_seconds() / 60.0,
        0.0,
    )
    rolling_window_start = normalized_end_at - timedelta(minutes=ROLLING_WINDOW_MINUTES)
    rolling_events = [
        event for event in events if event.created_at >= rolling_window_start
    ]
    rolling_request_count = len(rolling_events)
    rolling_token_count = sum(event.total_tokens for event in rolling_events)
    service_health = _build_service_health(
        events=events,
        end_at=normalized_end_at,
    )
    shown_events = events[:REQUEST_EVENT_RENDER_LIMIT]

    return {
        "generated_at": generated_at,
        "time_range": {
            "preset": preset,
            "start_at": normalized_start_at,
            "end_at": normalized_end_at,
        },
        "currency": {
            "code": currency_code,
            "symbol": currency_symbol,
        },
        "overview": {
            "total_requests": total_requests,
            "success_requests": success_requests,
            "failed_requests": failed_requests,
            "success_rate": _success_rate(
                success_count=success_requests,
                total_count=total_requests,
            ),
            "total_tokens": total_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "reasoning_tokens": reasoning_tokens,
            "average_rpm": round(
                total_requests / window_minutes if window_minutes > 0 else 0.0,
                3,
            ),
            "average_tpm": round(
                total_tokens / window_minutes if window_minutes > 0 else 0.0,
                3,
            ),
            "total_cost_micros": sum(event.total_cost_micros for event in events),
            "rolling_window_minutes": ROLLING_WINDOW_MINUTES,
            "rolling_request_count": rolling_request_count,
            "rolling_token_count": rolling_token_count,
            "rolling_rpm": round(
                rolling_request_count / ROLLING_WINDOW_MINUTES,
                3,
            ),
            "rolling_tpm": round(
                rolling_token_count / ROLLING_WINDOW_MINUTES,
                3,
            ),
        },
        "service_health": {
            "availability_percentage": (
                _success_rate(
                    success_count=success_requests,
                    total_count=total_requests,
                )
                if total_requests > 0
                else None
            ),
            "request_count": total_requests,
            "success_count": success_requests,
            "failed_count": failed_requests,
            **service_health,
        },
        "request_trends": {
            "hourly": _build_request_trend_series(
                events=events,
                start_at=normalized_start_at,
                end_at=normalized_end_at,
                granularity="hour",
            ),
            "daily": _build_request_trend_series(
                events=events,
                start_at=normalized_start_at,
                end_at=normalized_end_at,
                granularity="day",
            ),
        },
        "token_usage_trends": {
            "hourly": _build_token_trend_series(
                events=events,
                start_at=normalized_start_at,
                end_at=normalized_end_at,
                granularity="hour",
            ),
            "daily": _build_token_trend_series(
                events=events,
                start_at=normalized_start_at,
                end_at=normalized_end_at,
                granularity="day",
            ),
        },
        "token_type_breakdown": {
            "hourly": _build_token_type_breakdown(
                events=events,
                start_at=normalized_start_at,
                end_at=normalized_end_at,
                granularity="hour",
            ),
            "daily": _build_token_type_breakdown(
                events=events,
                start_at=normalized_start_at,
                end_at=normalized_end_at,
                granularity="day",
            ),
        },
        "cost_overview": _build_cost_overview(
            events=events,
            start_at=normalized_start_at,
            end_at=normalized_end_at,
        ),
        "endpoint_statistics": _build_endpoint_statistics(events),
        "model_statistics": _build_model_statistics(events),
        "request_events": {
            "total": total_requests,
            "shown_count": len(shown_events),
            "render_limit": REQUEST_EVENT_RENDER_LIMIT,
            "available_filters": _build_request_event_available_filters(shown_events),
            "items": [_event_request_row(event) for event in shown_events],
        },
        "proxy_api_key_statistics": _build_proxy_api_key_statistics(events),
    }


__all__ = ["UsageSnapshotPreset", "get_usage_snapshot"]
