import asyncio
from typing import Literal, Mapping, cast
from uuid import uuid4

import pytest
from fastapi import HTTPException


def _unique_suffix() -> str:
    return f"{int(asyncio.get_running_loop().time() * 1_000_000)}-{uuid4().hex[:8]}"


class TestDEF074_PricingTemplateUpdateCAS:
    @pytest.mark.asyncio
    async def test_non_pricing_updates_keep_pricing_version(self):
        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import Profile
        from app.routers.pricing_templates import (
            create_pricing_template,
            update_pricing_template,
        )
        from app.schemas.schemas import PricingTemplateCreate, PricingTemplateUpdate

        await get_engine().dispose()
        suffix = _unique_suffix()

        async with AsyncSessionLocal() as db:
            profile = Profile(
                name=f"DEF074 Profile {suffix}",
                is_active=False,
                is_default=False,
                is_editable=True,
                version=0,
            )
            db.add(profile)
            await db.flush()

            template = await create_pricing_template(
                body=PricingTemplateCreate(
                    name=f"DEF074 Template {suffix}",
                    description="Original description",
                    pricing_currency_code="USD",
                    input_price="1.50",
                    output_price="3.00",
                ),
                db=db,
                profile_id=profile.id,
            )
            original_updated_at = template.updated_at
            default_policy: Literal["MAP_TO_OUTPUT"] = "MAP_TO_OUTPUT"

            updated = await update_pricing_template(
                template_id=template.id,
                body=PricingTemplateUpdate(
                    expected_updated_at=template.updated_at,
                    description="Updated description",
                    pricing_currency_code=template.pricing_currency_code,
                    input_price=template.input_price,
                    output_price=template.output_price,
                    missing_special_token_price_policy=default_policy,
                ),
                db=db,
                profile_id=profile.id,
            )

            assert updated.version == 1
            assert updated.description == "Updated description"
            assert updated.updated_at > original_updated_at

    @pytest.mark.asyncio
    async def test_stale_pricing_template_update_returns_conflict(self):
        from app.core.database import AsyncSessionLocal, get_engine
        from app.models.models import Profile
        from app.routers.pricing_templates import (
            create_pricing_template,
            update_pricing_template,
        )
        from app.schemas.schemas import PricingTemplateCreate, PricingTemplateUpdate

        await get_engine().dispose()
        suffix = _unique_suffix()

        async with AsyncSessionLocal() as db:
            profile = Profile(
                name=f"DEF074 Conflict Profile {suffix}",
                is_active=False,
                is_default=False,
                is_editable=True,
                version=0,
            )
            db.add(profile)
            await db.flush()

            template = await create_pricing_template(
                body=PricingTemplateCreate(
                    name=f"DEF074 Conflict Template {suffix}",
                    pricing_currency_code="USD",
                    input_price="2.00",
                    output_price="4.00",
                ),
                db=db,
                profile_id=profile.id,
            )
            stale_updated_at = template.updated_at

            pricing_updated = await update_pricing_template(
                template_id=template.id,
                body=PricingTemplateUpdate(
                    expected_updated_at=stale_updated_at,
                    input_price="2.50",
                ),
                db=db,
                profile_id=profile.id,
            )
            assert pricing_updated.version == 2

            with pytest.raises(HTTPException) as exc_info:
                await update_pricing_template(
                    template_id=template.id,
                    body=PricingTemplateUpdate(
                        expected_updated_at=stale_updated_at,
                        description="Stale description",
                    ),
                    db=db,
                    profile_id=profile.id,
                )

            assert exc_info.value.status_code == 409
            detail = cast(Mapping[str, object], exc_info.value.detail)
            assert "changed" in str(detail.get("message")).lower()
            assert detail.get("current_version") == 2
            assert (
                detail.get("current_updated_at")
                == pricing_updated.updated_at.isoformat()
            )
