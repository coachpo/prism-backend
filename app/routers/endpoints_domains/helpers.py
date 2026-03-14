from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.time import utc_now
from app.models.models import Connection, Endpoint, Profile


async def ensure_unique_endpoint_name(
    db: AsyncSession,
    *,
    profile_id: int,
    endpoint_name: str,
    exclude_id: int | None = None,
) -> None:
    query = select(Endpoint).where(
        Endpoint.profile_id == profile_id,
        Endpoint.name == endpoint_name,
    )
    if exclude_id is not None:
        query = query.where(Endpoint.id != exclude_id)
    existing = (await db.execute(query)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Endpoint name '{endpoint_name}' already exists",
        )


async def lock_profile_row(db: AsyncSession, *, profile_id: int) -> None:
    await db.execute(
        select(Profile.id).where(Profile.id == profile_id).with_for_update()
    )


async def load_endpoint_or_404(
    db: AsyncSession,
    *,
    endpoint_id: int,
    profile_id: int,
) -> Endpoint:
    result = await db.execute(
        select(Endpoint).where(
            Endpoint.id == endpoint_id,
            Endpoint.profile_id == profile_id,
        )
    )
    endpoint = result.scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return endpoint


async def list_ordered_endpoints(
    db: AsyncSession,
    *,
    profile_id: int,
) -> list[Endpoint]:
    result = await db.execute(
        select(Endpoint)
        .where(Endpoint.profile_id == profile_id)
        .order_by(Endpoint.position.asc(), Endpoint.id.asc())
    )
    return list(result.scalars().all())


async def get_next_endpoint_position(db: AsyncSession, *, profile_id: int) -> int:
    result = await db.execute(
        select(func.max(Endpoint.position)).where(Endpoint.profile_id == profile_id)
    )
    max_position = result.scalar_one_or_none()
    if max_position is None:
        return 0
    return int(max_position) + 1


def normalize_endpoint_positions(endpoints: list[Endpoint]) -> None:
    now = utc_now()
    for index, endpoint in enumerate(endpoints):
        if endpoint.position == index:
            continue
        endpoint.position = index
        endpoint.updated_at = now


def build_duplicate_endpoint_name(
    source_name: str,
    existing_names: set[str],
) -> str:
    base_name = f"{source_name.strip()} copy"
    if base_name not in existing_names:
        return base_name

    suffix = 2
    while f"{base_name} {suffix}" in existing_names:
        suffix += 1
    return f"{base_name} {suffix}"


async def list_dependent_connection_ids(
    db: AsyncSession,
    *,
    profile_id: int,
    endpoint_id: int,
) -> list[int]:
    return list(
        (
            await db.execute(
                select(Connection.id).where(
                    Connection.profile_id == profile_id,
                    Connection.endpoint_id == endpoint_id,
                )
            )
        )
        .scalars()
        .all()
    )


async def list_endpoint_usage_rows(
    db: AsyncSession,
    *,
    profile_id: int,
    endpoint_id: int,
) -> list[Connection]:
    return list(
        (
            await db.execute(
                select(Connection)
                .options(selectinload(Connection.model_config_rel))
                .where(
                    Connection.endpoint_id == endpoint_id,
                    Connection.profile_id == profile_id,
                )
                .order_by(Connection.id.asc())
            )
        )
        .scalars()
        .all()
    )


async def renumber_endpoints_after_delete(
    db: AsyncSession,
    *,
    profile_id: int,
    deleted_position: int,
) -> None:
    remaining_endpoints = list(
        (
            await db.execute(
                select(Endpoint)
                .where(
                    Endpoint.profile_id == profile_id,
                    Endpoint.position > deleted_position,
                )
                .order_by(Endpoint.position.asc(), Endpoint.id.asc())
            )
        )
        .scalars()
        .all()
    )
    if not remaining_endpoints:
        return

    now = utc_now()
    for index, remaining_endpoint in enumerate(
        remaining_endpoints,
        start=deleted_position,
    ):
        remaining_endpoint.position = index
        remaining_endpoint.updated_at = now
    await db.flush()


__all__ = [
    "build_duplicate_endpoint_name",
    "ensure_unique_endpoint_name",
    "get_next_endpoint_position",
    "list_dependent_connection_ids",
    "list_endpoint_usage_rows",
    "list_ordered_endpoints",
    "load_endpoint_or_404",
    "lock_profile_row",
    "normalize_endpoint_positions",
    "renumber_endpoints_after_delete",
]
