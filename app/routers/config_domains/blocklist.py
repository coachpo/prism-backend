from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.dependencies import get_db, get_effective_profile_id
from app.models.models import HeaderBlocklistRule
from app.schemas.schemas import (
    HeaderBlocklistRuleCreate,
    HeaderBlocklistRuleUpdate,
    HeaderBlocklistRuleResponse,
)

router = APIRouter()

@router.get(
    "/header-blocklist-rules",
    response_model=list[HeaderBlocklistRuleResponse],
)
async def list_header_blocklist_rules(
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
    include_disabled: bool = True,
):
    query = select(HeaderBlocklistRule).where(
        (HeaderBlocklistRule.is_system == True)  # noqa: E712
        | (HeaderBlocklistRule.profile_id == profile_id)
    ).order_by(
        HeaderBlocklistRule.is_system.desc(),
        HeaderBlocklistRule.id.asc(),
    )
    if not include_disabled:
        query = query.where(HeaderBlocklistRule.enabled == True)  # noqa: E712
    return (await db.execute(query)).scalars().all()


@router.get(
    "/header-blocklist-rules/{rule_id}",
    response_model=HeaderBlocklistRuleResponse,
)
async def get_header_blocklist_rule(
    rule_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    rule = (
        await db.execute(
            select(HeaderBlocklistRule).where(
                HeaderBlocklistRule.id == rule_id,
                (HeaderBlocklistRule.is_system == True)  # noqa: E712
                | (HeaderBlocklistRule.profile_id == profile_id),
            )
        )
    ).scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Header blocklist rule not found")
    return rule


@router.post(
    "/header-blocklist-rules",
    response_model=HeaderBlocklistRuleResponse,
    status_code=201,
)
async def create_header_blocklist_rule(
    body: HeaderBlocklistRuleCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    existing = (
        await db.execute(
            select(HeaderBlocklistRule).where(
                HeaderBlocklistRule.match_type == body.match_type,
                HeaderBlocklistRule.pattern == body.pattern,
                (HeaderBlocklistRule.is_system == True)  # noqa: E712
                | (HeaderBlocklistRule.profile_id == profile_id),
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Rule with match_type='{body.match_type}' and pattern='{body.pattern}' already exists",
        )

    rule = HeaderBlocklistRule(
        name=body.name,
        match_type=body.match_type,
        profile_id=profile_id,
        pattern=body.pattern,
        enabled=body.enabled,
        is_system=False,
    )
    db.add(rule)
    await db.flush()
    await db.refresh(rule)
    return rule


@router.patch(
    "/header-blocklist-rules/{rule_id}",
    response_model=HeaderBlocklistRuleResponse,
)
async def update_header_blocklist_rule(
    rule_id: int,
    body: HeaderBlocklistRuleUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    rule = (
        await db.execute(
            select(HeaderBlocklistRule).where(
                HeaderBlocklistRule.id == rule_id,
                (HeaderBlocklistRule.is_system == True)  # noqa: E712
                | (HeaderBlocklistRule.profile_id == profile_id),
            )
        )
    ).scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Header blocklist rule not found")

    update_data = body.model_dump(exclude_unset=True)

    if rule.is_system:
        immutable_fields = {"name", "match_type", "pattern"}
        attempted = immutable_fields & set(update_data.keys())
        if attempted:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot modify {', '.join(sorted(attempted))} on a system rule. Only 'enabled' is mutable.",
            )

    if "match_type" in update_data or "pattern" in update_data:
        new_match_type = update_data.get("match_type", rule.match_type)
        new_pattern = update_data.get("pattern", rule.pattern)
        if new_match_type == "prefix" and not new_pattern.endswith("-"):
            raise HTTPException(
                status_code=400,
                detail="prefix pattern must end with '-'",
            )
        existing = (
            await db.execute(
                select(HeaderBlocklistRule).where(
                    HeaderBlocklistRule.match_type == new_match_type,
                    HeaderBlocklistRule.pattern == new_pattern,
                    HeaderBlocklistRule.id != rule_id,
                    (HeaderBlocklistRule.is_system == True)  # noqa: E712
                    | (HeaderBlocklistRule.profile_id == profile_id),
                )
            )
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Rule with match_type='{new_match_type}' and pattern='{new_pattern}' already exists",
            )

    for field, value in update_data.items():
        setattr(rule, field, value)
    rule.updated_at = utc_now()

    await db.flush()
    await db.refresh(rule)
    return rule


@router.delete(
    "/header-blocklist-rules/{rule_id}",
)
async def delete_header_blocklist_rule(
    rule_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    profile_id: Annotated[int, Depends(get_effective_profile_id)],
):
    rule = (
        await db.execute(
            select(HeaderBlocklistRule).where(
                HeaderBlocklistRule.id == rule_id,
                HeaderBlocklistRule.profile_id == profile_id,
            )
        )
    ).scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Header blocklist rule not found")
    if rule.is_system:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete a system rule. Disable it instead.",
        )
    await db.delete(rule)
    await db.flush()
    return {"deleted": True}
