import asyncio
import logging
from datetime import timedelta

from sqlalchemy import case, func, select

from app.core.config import get_settings
from app.core.time import utc_now
from app.models.domains.routing import Connection, Endpoint, ModelConfig
from app.models.models import RequestLog
from app.services.background_tasks import background_task_manager
from app.schemas.domains.stats import (
    DashboardRealtimeUpdateResponse,
    DashboardRouteSnapshotResponse,
    RequestLogResponse,
    SpendingReportResponse,
    StatsSummaryResponse,
    ThroughputStatsResponse,
)
from app.services.realtime import connection_manager
from app.services.stats.spending import get_spending_report
from app.services.stats.summary import get_stats_summary
from app.services.stats.throughput import get_throughput_stats

logger = logging.getLogger(__name__)

_dashboard_update_latest_request_log_ids: dict[int, int] = {}
_dashboard_update_enqueued_profiles: set[int] = set()
_dashboard_update_debounce_tasks: dict[int, asyncio.Task[None]] = {}


async def shutdown_dashboard_update_lifecycle() -> None:
    tasks = list(_dashboard_update_debounce_tasks.values())
    _dashboard_update_debounce_tasks.clear()
    _dashboard_update_latest_request_log_ids.clear()
    _dashboard_update_enqueued_profiles.clear()

    for task in tasks:
        task.cancel()

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _enqueue_dashboard_update_worker(*, profile_id: int, request_log_id: int) -> None:
    async def run_broadcast() -> None:
        await _broadcast_coalesced_dashboard_updates(profile_id=profile_id)

    _dashboard_update_enqueued_profiles.add(profile_id)

    try:
        background_task_manager.enqueue(
            name=f"dashboard-update:{profile_id}:{request_log_id}",
            run=run_broadcast,
        )
    except Exception:
        _dashboard_update_enqueued_profiles.discard(profile_id)
        if _dashboard_update_latest_request_log_ids.get(profile_id) == request_log_id:
            _dashboard_update_latest_request_log_ids.pop(profile_id, None)
        raise


async def _debounce_dashboard_update_enqueue(*, profile_id: int) -> None:
    try:
        debounce_seconds = get_settings().dashboard_update_debounce_seconds
        if debounce_seconds > 0:
            await asyncio.sleep(debounce_seconds)

        request_log_id = _dashboard_update_latest_request_log_ids.get(profile_id)
        if request_log_id is None or profile_id in _dashboard_update_enqueued_profiles:
            return

        if not connection_manager.has_subscribers(
            profile_id=profile_id,
            channel="dashboard",
        ):
            _dashboard_update_latest_request_log_ids.pop(profile_id, None)
            return

        _enqueue_dashboard_update_worker(
            profile_id=profile_id,
            request_log_id=request_log_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Failed to debounce dashboard.update for profile_id=%d",
            profile_id,
        )
    finally:
        current_task = asyncio.current_task()
        if _dashboard_update_debounce_tasks.get(profile_id) is current_task:
            _dashboard_update_debounce_tasks.pop(profile_id, None)


async def build_dashboard_route_snapshot(
    *,
    db,
    entry: RequestLog,
    from_time,
    to_time,
) -> DashboardRouteSnapshotResponse | None:
    if entry.endpoint_id is None:
        return None

    success_case = case(
        (RequestLog.status_code.between(200, 299), 1),
        else_=0,
    )
    traffic_case = case(
        (RequestLog.success_flag.is_(True), 1),
        else_=0,
    )

    aggregate_row = (
        await db.execute(
            select(
                func.count().label("total_requests"),
                func.coalesce(func.sum(success_case), 0).label("success_count"),
                func.coalesce(func.sum(traffic_case), 0).label("traffic_count"),
            ).where(
                RequestLog.profile_id == entry.profile_id,
                RequestLog.model_id == entry.model_id,
                RequestLog.endpoint_id == entry.endpoint_id,
                RequestLog.created_at >= from_time,
                RequestLog.created_at <= to_time,
            )
        )
    ).one()

    metadata_row = None
    if entry.connection_id is not None:
        metadata_row = (
            await db.execute(
                select(
                    Connection.model_config_id.label("model_config_id"),
                    ModelConfig.display_name.label("model_display_name"),
                    ModelConfig.model_id.label("model_id"),
                    Endpoint.name.label("endpoint_name"),
                    Endpoint.base_url.label("endpoint_base_url"),
                )
                .join(ModelConfig, Connection.model_config_id == ModelConfig.id)
                .join(Endpoint, Connection.endpoint_id == Endpoint.id)
                .where(
                    Connection.id == entry.connection_id,
                    Connection.profile_id == entry.profile_id,
                )
            )
        ).one_or_none()

    if metadata_row is None:
        metadata_row = (
            await db.execute(
                select(
                    ModelConfig.id.label("model_config_id"),
                    ModelConfig.display_name.label("model_display_name"),
                    ModelConfig.model_id.label("model_id"),
                    Endpoint.name.label("endpoint_name"),
                    Endpoint.base_url.label("endpoint_base_url"),
                )
                .select_from(Connection)
                .join(ModelConfig, Connection.model_config_id == ModelConfig.id)
                .join(Endpoint, Connection.endpoint_id == Endpoint.id)
                .where(
                    Connection.profile_id == entry.profile_id,
                    Connection.endpoint_id == entry.endpoint_id,
                    ModelConfig.model_id == entry.model_id,
                )
                .order_by(Connection.is_active.desc(), Connection.priority.asc())
                .limit(1)
            )
        ).one_or_none()

    model_config_id = (
        int(metadata_row.model_config_id)
        if metadata_row is not None and metadata_row.model_config_id is not None
        else None
    )
    model_label = entry.model_id
    endpoint_label = (
        entry.endpoint_description
        or entry.endpoint_base_url
        or f"Endpoint {entry.endpoint_id}"
    )

    if metadata_row is not None:
        model_label = metadata_row.model_display_name or metadata_row.model_id
        endpoint_label = (
            metadata_row.endpoint_name
            or metadata_row.endpoint_base_url
            or endpoint_label
        )

    active_connection_count = 0
    if model_config_id is not None:
        active_connection_count = int(
            (
                await db.execute(
                    select(func.count(Connection.id)).where(
                        Connection.profile_id == entry.profile_id,
                        Connection.model_config_id == model_config_id,
                        Connection.endpoint_id == entry.endpoint_id,
                        Connection.is_active.is_(True),
                    )
                )
            ).scalar_one()
            or 0
        )

    request_count = int(aggregate_row.total_requests or 0)
    success_count = int(aggregate_row.success_count or 0)
    error_count = request_count - success_count
    traffic_count = int(aggregate_row.traffic_count or 0)

    return DashboardRouteSnapshotResponse(
        model_id=entry.model_id,
        model_config_id=model_config_id,
        model_label=model_label,
        endpoint_id=entry.endpoint_id,
        endpoint_label=endpoint_label,
        active_connection_count=active_connection_count,
        traffic_request_count_24h=traffic_count,
        request_count_24h=request_count,
        success_count_24h=success_count,
        error_count_24h=error_count,
        success_rate_24h=(
            round((success_count / request_count) * 100, 2)
            if request_count > 0
            else None
        ),
    )


async def build_dashboard_update_message(*, db, entry: RequestLog) -> dict:
    window_end = entry.created_at or utc_now()
    window_start_24h = window_end - timedelta(hours=24)

    request_log = RequestLogResponse.model_validate(entry)
    stats_summary = StatsSummaryResponse.model_validate(
        await get_stats_summary(
            db,
            profile_id=entry.profile_id,
            from_time=window_start_24h,
            to_time=window_end,
        )
    )
    provider_summary = StatsSummaryResponse.model_validate(
        await get_stats_summary(
            db,
            profile_id=entry.profile_id,
            from_time=window_start_24h,
            to_time=window_end,
            group_by="provider",
        )
    )
    spending_summary = SpendingReportResponse.model_validate(
        await get_spending_report(
            db,
            profile_id=entry.profile_id,
            preset="last_30_days",
            top_n=5,
        )
    )
    throughput = ThroughputStatsResponse.model_validate(
        await get_throughput_stats(
            db,
            profile_id=entry.profile_id,
            from_time=window_start_24h,
            to_time=window_end,
        )
    )
    routing_route = await build_dashboard_route_snapshot(
        db=db,
        entry=entry,
        from_time=window_start_24h,
        to_time=window_end,
    )

    update = DashboardRealtimeUpdateResponse(
        request_log=request_log,
        stats_summary_24h=stats_summary,
        provider_summary_24h=provider_summary,
        spending_summary_30d=spending_summary,
        throughput_24h=throughput,
        routing_route_24h=routing_route,
    )

    return {
        "type": "dashboard.update",
        **update.model_dump(mode="json"),
    }


async def broadcast_dashboard_update_for_request_log(
    *,
    request_log_id: int,
    profile_id: int,
) -> None:
    if not connection_manager.has_subscribers(
        profile_id=profile_id,
        channel="dashboard",
    ):
        return

    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        entry = await db.get(RequestLog, request_log_id)
        if entry is None:
            logger.warning(
                "Skipping dashboard.update for missing request log: profile_id=%d request_log_id=%d",
                profile_id,
                request_log_id,
            )
            return
        if entry.profile_id != profile_id:
            logger.warning(
                "Skipping dashboard.update for mismatched request log profile: expected_profile_id=%d actual_profile_id=%d request_log_id=%d",
                profile_id,
                entry.profile_id,
                request_log_id,
            )
            return

        dashboard_message = await build_dashboard_update_message(db=db, entry=entry)
        await connection_manager.broadcast_to_profile(
            profile_id=profile_id,
            channel="dashboard",
            message=dashboard_message,
        )


async def _broadcast_coalesced_dashboard_updates(*, profile_id: int) -> None:
    last_attempted_request_log_id: int | None = None
    requeue_request_log_id: int | None = None

    try:
        while True:
            request_log_id = _dashboard_update_latest_request_log_ids.get(profile_id)
            if request_log_id is None:
                return

            last_attempted_request_log_id = request_log_id

            try:
                await broadcast_dashboard_update_for_request_log(
                    request_log_id=request_log_id,
                    profile_id=profile_id,
                )
            except Exception:
                latest_request_log_id = _dashboard_update_latest_request_log_ids.get(
                    profile_id
                )
                if latest_request_log_id not in (None, request_log_id):
                    requeue_request_log_id = latest_request_log_id
                raise

            if (
                _dashboard_update_latest_request_log_ids.get(profile_id)
                == request_log_id
            ):
                return
    finally:
        latest_request_log_id = _dashboard_update_latest_request_log_ids.get(profile_id)
        if latest_request_log_id in (None, last_attempted_request_log_id):
            _dashboard_update_latest_request_log_ids.pop(profile_id, None)
        _dashboard_update_enqueued_profiles.discard(profile_id)
        if requeue_request_log_id is not None:
            enqueue_dashboard_update_broadcast(
                request_log_id=requeue_request_log_id,
                profile_id=profile_id,
            )


def enqueue_dashboard_update_broadcast(
    *,
    request_log_id: int,
    profile_id: int,
) -> None:
    _dashboard_update_latest_request_log_ids[profile_id] = request_log_id

    if profile_id in _dashboard_update_enqueued_profiles:
        return

    debounce_seconds = get_settings().dashboard_update_debounce_seconds
    if debounce_seconds > 0 and profile_id in _dashboard_update_debounce_tasks:
        return

    if not connection_manager.has_subscribers(
        profile_id=profile_id,
        channel="dashboard",
    ):
        _dashboard_update_latest_request_log_ids.pop(profile_id, None)
        return

    if debounce_seconds > 0:
        _dashboard_update_debounce_tasks[profile_id] = asyncio.create_task(
            _debounce_dashboard_update_enqueue(profile_id=profile_id)
        )
        return

    _enqueue_dashboard_update_worker(
        profile_id=profile_id,
        request_log_id=request_log_id,
    )


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
                enqueue_dashboard_update_broadcast(
                    request_log_id=entry.id,
                    profile_id=entry.profile_id,
                )
            except Exception:
                logger.exception(
                    "Failed to enqueue dashboard.update payload for request_log_id=%s",
                    entry.id,
                )

            return entry.id
    except asyncio.CancelledError:
        logger.debug("Request logging cancelled")
        return None
    except Exception:
        logger.exception("Failed to log request")
        return None
