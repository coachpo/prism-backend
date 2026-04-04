import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.sql.dml import Delete

from app.core.database import AsyncSessionLocal
from app.core.time import utc_now
from app.models.models import AuditLog, LoadbalanceEvent, RequestLog, UsageRequestEvent

logger = logging.getLogger(__name__)


async def _run_delete(*, stmt: Delete, task_name: str) -> None:
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(stmt)
            await session.commit()
    except asyncio.CancelledError:
        logger.debug("%s cancelled", task_name)
    except Exception:
        logger.exception("%s failed", task_name)


async def delete_request_logs_in_background(
    *,
    profile_id: int,
    older_than_days: int | None,
    delete_all: bool,
) -> None:
    if delete_all:
        stmt = delete(RequestLog).where(RequestLog.profile_id == profile_id)
    else:
        if older_than_days is None:
            logger.debug(
                "Skipping background request-log delete for profile_id=%d without criteria",
                profile_id,
            )
            return
        cutoff = utc_now() - timedelta(days=older_than_days)
        stmt = delete(RequestLog).where(
            RequestLog.profile_id == profile_id,
            RequestLog.created_at < cutoff,
        )

    await _run_delete(
        stmt=stmt,
        task_name=f"background request-log delete for profile_id={profile_id}",
    )


async def delete_audit_logs_in_background(
    *,
    profile_id: int,
    before: datetime | None,
    older_than_days: int | None,
    delete_all: bool,
) -> None:
    if delete_all:
        stmt = delete(AuditLog).where(AuditLog.profile_id == profile_id)
    elif older_than_days is not None:
        cutoff = utc_now() - timedelta(days=older_than_days)
        stmt = delete(AuditLog).where(
            AuditLog.profile_id == profile_id,
            AuditLog.created_at < cutoff,
        )
    elif before is not None:
        stmt = delete(AuditLog).where(
            AuditLog.profile_id == profile_id,
            AuditLog.created_at < before,
        )
    else:
        logger.debug(
            "Skipping background audit-log delete for profile_id=%d without criteria",
            profile_id,
        )
        return

    await _run_delete(
        stmt=stmt,
        task_name=f"background audit-log delete for profile_id={profile_id}",
    )


async def delete_statistics_in_background(
    *,
    profile_id: int,
    older_than_days: int | None,
    delete_all: bool,
) -> None:
    if delete_all:
        stmt = delete(UsageRequestEvent).where(UsageRequestEvent.profile_id == profile_id)
    else:
        if older_than_days is None:
            logger.debug(
                "Skipping background statistics delete for profile_id=%d without criteria",
                profile_id,
            )
            return
        cutoff = utc_now() - timedelta(days=older_than_days)
        stmt = delete(UsageRequestEvent).where(
            UsageRequestEvent.profile_id == profile_id,
            UsageRequestEvent.created_at < cutoff,
        )

    await _run_delete(
        stmt=stmt,
        task_name=f"background statistics delete for profile_id={profile_id}",
    )


async def delete_loadbalance_events_in_background(
    *,
    profile_id: int,
    before: datetime | None,
    older_than_days: int | None,
    delete_all: bool,
) -> None:
    if delete_all:
        stmt = delete(LoadbalanceEvent).where(LoadbalanceEvent.profile_id == profile_id)
    elif older_than_days is not None:
        cutoff = utc_now() - timedelta(days=older_than_days)
        stmt = delete(LoadbalanceEvent).where(
            LoadbalanceEvent.profile_id == profile_id,
            LoadbalanceEvent.created_at < cutoff,
        )
    elif before is not None:
        stmt = delete(LoadbalanceEvent).where(
            LoadbalanceEvent.profile_id == profile_id,
            LoadbalanceEvent.created_at < before,
        )
    else:
        logger.debug(
            "Skipping background loadbalance-event delete for profile_id=%d without criteria",
            profile_id,
        )
        return

    await _run_delete(
        stmt=stmt,
        task_name=f"background loadbalance-event delete for profile_id={profile_id}",
    )
