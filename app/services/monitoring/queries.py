from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.time import utc_now
from app.models.models import (
    Connection,
    ModelConfig,
    MonitoringConnectionProbeResult,
    RoutingConnectionRuntimeState,
    Vendor,
)
from app.schemas.schemas import (
    MonitoringConnectionHistoryItem,
    MonitoringModelConnectionRow,
    MonitoringModelResponse,
    MonitoringOverviewConnectionRow,
    MonitoringOverviewModelItem,
    MonitoringOverviewResponse,
    MonitoringOverviewVendorItem,
    MonitoringVendorModelItem,
    MonitoringVendorResponse,
)

_HISTORY_LIMIT = 60
_OVERVIEW_HISTORY_LIMIT = 60
_KNOWN_FUSED_STATUSES = {"healthy", "degraded", "unhealthy"}


@dataclass(frozen=True, slots=True)
class _MonitoringConnectionBundle:
    connection: Connection
    runtime_state: RoutingConnectionRuntimeState | None
    recent_history: list[MonitoringConnectionProbeResult]


def _normalize_fused_status(value: str | None) -> str | None:
    if value in _KNOWN_FUSED_STATUSES:
        return value
    return None


def _derive_history_fused_status(
    history_row: MonitoringConnectionProbeResult | None,
) -> str | None:
    if history_row is None:
        return None
    if (
        history_row.endpoint_ping_status == "healthy"
        and history_row.conversation_status == "healthy"
    ):
        return "healthy"
    if (
        history_row.endpoint_ping_status == "healthy"
        or history_row.conversation_status == "healthy"
    ):
        return "degraded"
    return "unhealthy"


def _round_metric(value: Decimal | float | int | None) -> int | None:
    if value is None:
        return None
    return int(round(float(value)))


def _derive_connection_fused_status(bundle: _MonitoringConnectionBundle) -> str:
    runtime_status = _normalize_fused_status(
        bundle.runtime_state.last_probe_status
        if bundle.runtime_state is not None
        else None
    )
    if runtime_status is not None:
        return runtime_status

    history_status = _derive_history_fused_status(
        bundle.recent_history[0] if bundle.recent_history else None
    )
    if history_status is not None:
        return history_status

    if bundle.connection.health_status == "healthy":
        return "healthy"
    if bundle.connection.health_status == "unhealthy":
        return "unhealthy"
    return "unknown"


def _roll_up_group_status(statuses: list[str]) -> str:
    if not statuses:
        return "unknown"
    if any(status not in _KNOWN_FUSED_STATUSES for status in statuses):
        normalized = [status for status in statuses if status in _KNOWN_FUSED_STATUSES]
        if not normalized:
            return "unknown"
        statuses = normalized

    if "healthy" in statuses and any(status != "healthy" for status in statuses):
        return "degraded"
    if "healthy" in statuses:
        return "healthy"
    if "degraded" in statuses:
        return "degraded"
    if "unhealthy" in statuses:
        return "unhealthy"
    return "unknown"


def _build_connection_payload(
    bundle: _MonitoringConnectionBundle,
    *,
    history_oldest_first: bool = False,
) -> tuple[dict[str, object], datetime | None]:
    history_rows = (
        list(reversed(bundle.recent_history))
        if history_oldest_first
        else bundle.recent_history
    )
    latest_history = (
        (history_rows[-1] if history_oldest_first else history_rows[0])
        if history_rows
        else None
    )
    fused_status = _derive_connection_fused_status(bundle)
    runtime_state = bundle.runtime_state
    last_probe_status = _normalize_fused_status(
        runtime_state.last_probe_status if runtime_state is not None else None
    ) or _derive_history_fused_status(latest_history)
    last_probe_at = (
        runtime_state.last_probe_at if runtime_state is not None else None
    ) or (latest_history.checked_at if latest_history is not None else None)

    endpoint_ping_status = (
        latest_history.endpoint_ping_status
        if latest_history is not None
        else fused_status
        if fused_status in _KNOWN_FUSED_STATUSES
        else "unknown"
    )
    conversation_status = (
        latest_history.conversation_status
        if latest_history is not None
        else fused_status
        if fused_status in _KNOWN_FUSED_STATUSES
        else "unknown"
    )
    endpoint_ping_ms = (
        latest_history.endpoint_ping_ms
        if latest_history is not None
        else _round_metric(
            runtime_state.endpoint_ping_ewma_ms if runtime_state is not None else None
        )
    )
    conversation_delay_ms = (
        latest_history.conversation_delay_ms
        if latest_history is not None
        else _round_metric(
            runtime_state.conversation_delay_ewma_ms
            if runtime_state is not None
            else None
        )
    )

    payload = {
        "connection_id": bundle.connection.id,
        "connection_name": bundle.connection.name,
        "endpoint_id": bundle.connection.endpoint_rel.id,
        "endpoint_name": bundle.connection.endpoint_rel.name,
        "last_probe_status": last_probe_status,
        "circuit_state": (
            runtime_state.circuit_state if runtime_state is not None else None
        ),
        "live_p95_latency_ms": _round_metric(
            runtime_state.live_p95_latency_ms if runtime_state is not None else None
        ),
        "last_live_failure_kind": (
            runtime_state.last_live_failure_kind if runtime_state is not None else None
        ),
        "last_live_failure_at": (
            runtime_state.last_live_failure_at if runtime_state is not None else None
        ),
        "last_live_success_at": (
            runtime_state.last_live_success_at if runtime_state is not None else None
        ),
        "endpoint_ping_status": endpoint_ping_status,
        "endpoint_ping_ms": endpoint_ping_ms,
        "conversation_status": conversation_status,
        "conversation_delay_ms": conversation_delay_ms,
        "fused_status": fused_status,
        "recent_history": [
            MonitoringConnectionHistoryItem(
                checked_at=row.checked_at,
                endpoint_ping_status=row.endpoint_ping_status,
                endpoint_ping_ms=row.endpoint_ping_ms,
                conversation_status=row.conversation_status,
                conversation_delay_ms=row.conversation_delay_ms,
                failure_kind=row.failure_kind,
            )
            for row in history_rows
        ],
    }

    return payload, last_probe_at


def _build_overview_connection_row(
    bundle: _MonitoringConnectionBundle,
    *,
    history_oldest_first: bool = False,
) -> MonitoringOverviewConnectionRow:
    payload, last_probe_at = _build_connection_payload(
        bundle,
        history_oldest_first=history_oldest_first,
    )

    return MonitoringOverviewConnectionRow(
        **payload,
        monitoring_probe_interval_seconds=bundle.connection.monitoring_probe_interval_seconds,
        last_probe_at=last_probe_at,
    )


def _build_model_connection_row(
    bundle: _MonitoringConnectionBundle,
    *,
    history_oldest_first: bool = False,
) -> MonitoringModelConnectionRow:
    payload, _ = _build_connection_payload(
        bundle,
        history_oldest_first=history_oldest_first,
    )

    return MonitoringModelConnectionRow(**payload)


def _build_overview_model_item(
    bundles: list[_MonitoringConnectionBundle],
) -> MonitoringOverviewModelItem:
    model = bundles[0].connection.model_config_rel
    connection_rows = [_build_overview_connection_row(bundle) for bundle in bundles]
    return MonitoringOverviewModelItem(
        model_config_id=model.id,
        model_id=model.model_id,
        display_name=model.display_name,
        fused_status=_roll_up_group_status(
            [row.fused_status for row in connection_rows]
        ),
        connection_count=len(connection_rows),
        connections=connection_rows,
    )


def _build_connection_query(
    *,
    profile_id: int,
    vendor_id: int | None = None,
    model_config_id: int | None = None,
) -> Select[tuple[Connection]]:
    stmt = (
        select(Connection)
        .options(
            selectinload(Connection.endpoint_rel),
            selectinload(Connection.model_config_rel).selectinload(ModelConfig.vendor),
        )
        .join(ModelConfig, ModelConfig.id == Connection.model_config_id)
        .join(Vendor, Vendor.id == ModelConfig.vendor_id)
        .where(
            Connection.profile_id == profile_id,
            Connection.is_active.is_(True),
            ModelConfig.profile_id == profile_id,
            ModelConfig.is_enabled.is_(True),
        )
        .order_by(
            Vendor.name.asc(),
            ModelConfig.model_id.asc(),
            Connection.priority.asc(),
            Connection.id.asc(),
        )
    )
    if vendor_id is not None:
        stmt = stmt.where(ModelConfig.vendor_id == vendor_id)
    if model_config_id is not None:
        stmt = stmt.where(Connection.model_config_id == model_config_id)
    return stmt


async def _load_monitored_connections(
    db: AsyncSession,
    *,
    profile_id: int,
    vendor_id: int | None = None,
    model_config_id: int | None = None,
) -> list[Connection]:
    result = await db.execute(
        _build_connection_query(
            profile_id=profile_id,
            vendor_id=vendor_id,
            model_config_id=model_config_id,
        )
    )
    return list(result.scalars().all())


async def _load_runtime_state_by_connection(
    db: AsyncSession,
    *,
    profile_id: int,
    connection_ids: list[int],
) -> dict[int, RoutingConnectionRuntimeState]:
    if not connection_ids:
        return {}

    result = await db.execute(
        select(RoutingConnectionRuntimeState).where(
            RoutingConnectionRuntimeState.profile_id == profile_id,
            RoutingConnectionRuntimeState.connection_id.in_(connection_ids),
        )
    )
    rows = list(result.scalars().all())
    return {row.connection_id: row for row in rows}


async def _load_recent_history_by_connection(
    db: AsyncSession,
    *,
    profile_id: int,
    connection_ids: list[int],
    history_limit: int,
    oldest_first: bool = False,
) -> dict[int, list[MonitoringConnectionProbeResult]]:
    if not connection_ids:
        return {}

    result = await db.execute(
        select(MonitoringConnectionProbeResult)
        .where(
            MonitoringConnectionProbeResult.profile_id == profile_id,
            MonitoringConnectionProbeResult.connection_id.in_(connection_ids),
        )
        .order_by(
            MonitoringConnectionProbeResult.connection_id.asc(),
            MonitoringConnectionProbeResult.checked_at.desc(),
            MonitoringConnectionProbeResult.id.desc(),
        )
    )
    grouped: dict[int, list[MonitoringConnectionProbeResult]] = defaultdict(list)
    for row in result.scalars().all():
        bucket = grouped[row.connection_id]
        if len(bucket) < history_limit:
            bucket.append(row)
    if oldest_first:
        for bucket in grouped.values():
            bucket.reverse()
    return grouped


async def _load_connection_bundles(
    db: AsyncSession,
    *,
    profile_id: int,
    connections: list[Connection],
    history_limit: int = _HISTORY_LIMIT,
    history_oldest_first: bool = False,
) -> list[_MonitoringConnectionBundle]:
    connection_ids = [connection.id for connection in connections]
    runtime_state_by_connection = await _load_runtime_state_by_connection(
        db,
        profile_id=profile_id,
        connection_ids=connection_ids,
    )
    history_by_connection = await _load_recent_history_by_connection(
        db,
        profile_id=profile_id,
        connection_ids=connection_ids,
        history_limit=history_limit,
        oldest_first=history_oldest_first,
    )
    return [
        _MonitoringConnectionBundle(
            connection=connection,
            runtime_state=runtime_state_by_connection.get(connection.id),
            recent_history=history_by_connection.get(connection.id, []),
        )
        for connection in connections
    ]


async def _load_vendor_or_404(
    db: AsyncSession,
    *,
    profile_id: int,
    vendor_id: int,
) -> Vendor:
    result = await db.execute(
        select(Vendor)
        .join(ModelConfig, ModelConfig.vendor_id == Vendor.id)
        .where(
            Vendor.id == vendor_id,
            ModelConfig.profile_id == profile_id,
            ModelConfig.is_enabled.is_(True),
        )
    )
    vendor = result.scalars().first()
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return vendor


async def _load_model_or_404(
    db: AsyncSession,
    *,
    profile_id: int,
    model_config_id: int,
) -> ModelConfig:
    result = await db.execute(
        select(ModelConfig)
        .options(selectinload(ModelConfig.vendor))
        .where(
            ModelConfig.id == model_config_id,
            ModelConfig.profile_id == profile_id,
            ModelConfig.is_enabled.is_(True),
        )
    )
    model = result.scalar_one_or_none()
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found")
    return model


async def query_monitoring_overview(
    *,
    db: AsyncSession,
    profile_id: int,
) -> MonitoringOverviewResponse:
    generated_at = utc_now()
    connections = await _load_monitored_connections(db, profile_id=profile_id)
    bundles = await _load_connection_bundles(
        db,
        profile_id=profile_id,
        connections=connections,
        history_limit=_OVERVIEW_HISTORY_LIMIT,
    )
    bundles_by_vendor: dict[int, list[_MonitoringConnectionBundle]] = defaultdict(list)
    for bundle in bundles:
        bundles_by_vendor[bundle.connection.model_config_rel.vendor.id].append(bundle)

    vendor_items: list[MonitoringOverviewVendorItem] = []
    for vendor_id, vendor_bundles in bundles_by_vendor.items():
        vendor = vendor_bundles[0].connection.model_config_rel.vendor
        bundles_by_model: dict[int, list[_MonitoringConnectionBundle]] = defaultdict(
            list
        )
        for bundle in vendor_bundles:
            bundles_by_model[bundle.connection.model_config_id].append(bundle)

        model_items = [
            _build_overview_model_item(grouped_bundles)
            for grouped_bundles in bundles_by_model.values()
        ]
        model_items.sort(key=lambda item: (item.model_id.lower(), item.model_config_id))

        statuses = [
            row.fused_status
            for model_item in model_items
            for row in model_item.connections
        ]
        vendor_items.append(
            MonitoringOverviewVendorItem(
                vendor_id=vendor_id,
                vendor_key=vendor.key,
                vendor_name=vendor.name,
                icon_key=vendor.icon_key,
                fused_status=_roll_up_group_status(
                    [model_item.fused_status for model_item in model_items]
                ),
                model_count=len(model_items),
                connection_count=len(vendor_bundles),
                healthy_connection_count=sum(
                    1 for status in statuses if status == "healthy"
                ),
                degraded_connection_count=sum(
                    1 for status in statuses if status != "healthy"
                ),
                models=model_items,
            )
        )

    vendor_items.sort(key=lambda item: (item.vendor_name.lower(), item.vendor_id))
    return MonitoringOverviewResponse(generated_at=generated_at, vendors=vendor_items)


async def query_monitoring_vendor(
    *,
    db: AsyncSession,
    profile_id: int,
    vendor_id: int,
) -> MonitoringVendorResponse:
    generated_at = utc_now()
    vendor = await _load_vendor_or_404(db, profile_id=profile_id, vendor_id=vendor_id)
    connections = await _load_monitored_connections(
        db,
        profile_id=profile_id,
        vendor_id=vendor_id,
    )
    bundles = await _load_connection_bundles(
        db,
        profile_id=profile_id,
        connections=connections,
    )
    bundles_by_model: dict[int, list[_MonitoringConnectionBundle]] = defaultdict(list)
    for bundle in bundles:
        bundles_by_model[bundle.connection.model_config_id].append(bundle)

    model_items: list[MonitoringVendorModelItem] = []
    for grouped_bundles in bundles_by_model.values():
        model = grouped_bundles[0].connection.model_config_rel
        model_items.append(
            MonitoringVendorModelItem(
                model_config_id=model.id,
                model_id=model.model_id,
                display_name=model.display_name,
                fused_status=_roll_up_group_status(
                    [
                        _derive_connection_fused_status(bundle)
                        for bundle in grouped_bundles
                    ]
                ),
                connection_count=len(grouped_bundles),
            )
        )

    model_items.sort(key=lambda item: (item.model_id.lower(), item.model_config_id))
    return MonitoringVendorResponse(
        generated_at=generated_at,
        vendor_id=vendor.id,
        vendor_key=vendor.key,
        vendor_name=vendor.name,
        models=model_items,
    )


async def query_monitoring_model(
    *,
    db: AsyncSession,
    profile_id: int,
    model_config_id: int,
) -> MonitoringModelResponse:
    generated_at = utc_now()
    model = await _load_model_or_404(
        db,
        profile_id=profile_id,
        model_config_id=model_config_id,
    )
    connections = await _load_monitored_connections(
        db,
        profile_id=profile_id,
        model_config_id=model_config_id,
    )
    bundles = await _load_connection_bundles(
        db,
        profile_id=profile_id,
        connections=connections,
        history_limit=_OVERVIEW_HISTORY_LIMIT,
    )
    connection_rows = [_build_model_connection_row(bundle) for bundle in bundles]

    return MonitoringModelResponse(
        generated_at=generated_at,
        vendor_id=model.vendor.id,
        vendor_key=model.vendor.key,
        vendor_name=model.vendor.name,
        model_config_id=model.id,
        model_id=model.model_id,
        display_name=model.display_name,
        connections=connection_rows,
    )


__all__ = [
    "query_monitoring_model",
    "query_monitoring_overview",
    "query_monitoring_vendor",
]
