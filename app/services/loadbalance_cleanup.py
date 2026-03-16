import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete

from app.core.database import AsyncSessionLocal
from app.core.time import utc_now
from app.models.models import LoadbalanceEvent

logger = logging.getLogger(__name__)


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

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(stmt)
            await session.commit()
    except asyncio.CancelledError:
        logger.debug("background loadbalance-event delete for profile_id=%d cancelled", profile_id)
    except Exception:
        logger.exception("background loadbalance-event delete for profile_id=%d failed", profile_id)
