from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.database import AsyncSessionLocal
from app.models.models import Profile, RequestLog


def _request_log(
    *,
    profile_id: int,
    response_time_ms: int,
    created_at: datetime,
    status_code: int = 200,
    api_family: str = "openai",
    resolved_target_model_id: str | None = None,
    ingress_request_id: str | None = None,
    attempt_number: int | None = None,
    provider_correlation_id: str | None = None,
    success_flag: bool | None = None,
    billable_flag: bool | None = None,
    priced_flag: bool | None = None,
    total_cost_user_currency_micros: int | None = None,
) -> RequestLog:
    return RequestLog(
        profile_id=profile_id,
        model_id="gpt-test",
        api_family=api_family,
        resolved_target_model_id=resolved_target_model_id,
        endpoint_id=None,
        connection_id=None,
        ingress_request_id=ingress_request_id,
        attempt_number=attempt_number,
        provider_correlation_id=provider_correlation_id,
        endpoint_base_url="https://api.openai.com",
        status_code=status_code,
        response_time_ms=response_time_ms,
        is_stream=False,
        request_path="/v1/chat/completions",
        success_flag=success_flag,
        billable_flag=billable_flag,
        priced_flag=priced_flag,
        total_cost_user_currency_micros=total_cost_user_currency_micros,
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_get_stats_summary_reads_p95_from_sql_aggregate_query() -> None:
    from app.services.stats.summary import get_stats_summary

    aggregate_result = MagicMock()
    aggregate_result.one.return_value = SimpleNamespace(
        total_requests=20,
        success_count=19,
        avg_response_time_ms=12.5,
        p95_response_time_ms=20,
        total_input_tokens=0,
        total_output_tokens=0,
        total_tokens=0,
    )
    legacy_p95_result = MagicMock()
    legacy_p95_result.all.return_value = []

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[aggregate_result, legacy_p95_result])

    summary = await get_stats_summary(db, profile_id=7)

    assert summary["p95_response_time_ms"] == 20
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_get_stats_summary_uses_postgresql_percentile_cont_semantics() -> None:
    from app.services.stats.summary import get_stats_summary

    created_at = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)

    async with AsyncSessionLocal() as db:
        primary_profile = Profile(
            name=f"summary-profile-{uuid4()}",
            is_active=False,
            is_default=False,
        )
        other_profile = Profile(
            name=f"summary-other-profile-{uuid4()}",
            is_active=False,
            is_default=False,
        )
        db.add_all([primary_profile, other_profile])
        await db.flush()

        db.add_all(
            [
                _request_log(
                    profile_id=primary_profile.id,
                    response_time_ms=response_time_ms,
                    created_at=created_at,
                )
                for response_time_ms in range(1, 21)
            ]
            + [
                _request_log(
                    profile_id=other_profile.id,
                    response_time_ms=9_999,
                    created_at=created_at,
                )
            ]
        )
        await db.commit()

        summary = await get_stats_summary(db, profile_id=primary_profile.id)

    assert summary["total_requests"] == 20
    assert summary["p95_response_time_ms"] == 19


@pytest.mark.asyncio
async def test_get_request_logs_uses_stable_id_tiebreaker_for_timestamp_sort() -> None:
    from app.services.stats.request_logs import get_request_logs

    count_result = MagicMock()
    count_result.scalar.return_value = 0
    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = []
    statements = []

    async def capture_execute(statement):
        statements.append(statement)
        return count_result if len(statements) == 1 else rows_result

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=capture_execute)

    await get_request_logs(db, profile_id=7, limit=50, offset=0)

    order_clauses = [str(clause) for clause in statements[1]._order_by_clauses]
    assert order_clauses == [
        str(RequestLog.created_at.desc()),
        str(RequestLog.id.desc()),
    ]


def test_operations_request_logs_contract_is_retired_from_stats_services() -> None:
    import app.services.stats.request_logs as request_logs
    import app.services.stats_service as stats_service

    legacy_name = "get_" + "operations_request_logs"

    assert not hasattr(request_logs, legacy_name)
    assert not hasattr(stats_service, legacy_name)


@pytest.mark.asyncio
async def test_get_request_logs_filters_by_status_family_and_preserves_failure_filter() -> (
    None
):
    from app.services.stats.request_logs import get_request_logs

    created_at = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)

    async with AsyncSessionLocal() as db:
        primary_profile = Profile(
            name=f"request-log-profile-{uuid4()}",
            is_active=False,
            is_default=False,
        )
        other_profile = Profile(
            name=f"request-log-other-profile-{uuid4()}",
            is_active=False,
            is_default=False,
        )
        db.add_all([primary_profile, other_profile])
        await db.flush()

        db.add_all(
            [
                _request_log(
                    profile_id=primary_profile.id,
                    response_time_ms=100,
                    created_at=created_at,
                    status_code=404,
                ),
                _request_log(
                    profile_id=primary_profile.id,
                    response_time_ms=110,
                    created_at=created_at,
                    status_code=429,
                ),
                _request_log(
                    profile_id=primary_profile.id,
                    response_time_ms=120,
                    created_at=created_at,
                    status_code=500,
                ),
                _request_log(
                    profile_id=primary_profile.id,
                    response_time_ms=130,
                    created_at=created_at,
                    status_code=200,
                ),
                _request_log(
                    profile_id=other_profile.id,
                    response_time_ms=140,
                    created_at=created_at,
                    status_code=418,
                ),
            ]
        )
        await db.commit()

        items, total = await get_request_logs(
            db,
            profile_id=primary_profile.id,
            status_family="4xx",
            success=False,
            limit=50,
            offset=0,
        )

    assert total == 2
    assert [item.status_code for item in items] == [429, 404]


@pytest.mark.asyncio
async def test_get_request_logs_filters_by_ingress_request_id_and_preserves_attempt_order() -> (
    None
):
    from app.services.stats.request_logs import get_request_logs

    created_at = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)

    async with AsyncSessionLocal() as db:
        profile = Profile(
            name=f"ingress-log-profile-{uuid4()}",
            is_active=False,
            is_default=False,
        )
        other_profile = Profile(
            name=f"ingress-log-other-profile-{uuid4()}",
            is_active=False,
            is_default=False,
        )
        db.add_all([profile, other_profile])
        await db.flush()

        db.add_all(
            [
                _request_log(
                    profile_id=profile.id,
                    response_time_ms=100,
                    created_at=created_at,
                    ingress_request_id="ingress-123",
                    attempt_number=1,
                    provider_correlation_id="resp-1",
                ),
                _request_log(
                    profile_id=profile.id,
                    response_time_ms=110,
                    created_at=created_at,
                    ingress_request_id="ingress-123",
                    attempt_number=2,
                    provider_correlation_id="resp-2",
                    status_code=503,
                ),
                _request_log(
                    profile_id=profile.id,
                    response_time_ms=120,
                    created_at=created_at,
                    ingress_request_id="other-ingress",
                    attempt_number=1,
                ),
                _request_log(
                    profile_id=other_profile.id,
                    response_time_ms=130,
                    created_at=created_at,
                    ingress_request_id="ingress-123",
                    attempt_number=9,
                ),
            ]
        )
        await db.commit()

        items, total = await get_request_logs(
            db,
            profile_id=profile.id,
            ingress_request_id="ingress-123",
            limit=50,
            offset=0,
        )

    assert total == 2
    assert [item.attempt_number for item in items] == [2, 1]
    assert [item.provider_correlation_id for item in items] == ["resp-2", "resp-1"]


@pytest.mark.asyncio
async def test_get_request_logs_preserves_resolved_target_model_id() -> None:
    from app.services.stats.request_logs import get_request_logs

    created_at = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)

    async with AsyncSessionLocal() as db:
        profile = Profile(
            name=f"resolved-target-profile-{uuid4()}",
            is_active=False,
            is_default=False,
        )
        db.add(profile)
        await db.flush()

        db.add(
            _request_log(
                profile_id=profile.id,
                response_time_ms=100,
                created_at=created_at,
                resolved_target_model_id="target-model-a",
            )
        )
        await db.commit()

        items, total = await get_request_logs(
            db,
            profile_id=profile.id,
            limit=50,
            offset=0,
        )

    assert total == 1
    assert items[0].resolved_target_model_id == "target-model-a"


@pytest.mark.asyncio
async def test_get_request_logs_filters_by_api_family() -> None:
    from app.services.stats.request_logs import get_request_logs

    created_at = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)

    async with AsyncSessionLocal() as db:
        profile = Profile(
            name=f"api-family-filter-profile-{uuid4()}",
            is_active=False,
            is_default=False,
        )
        db.add(profile)
        await db.flush()

        db.add_all(
            [
                _request_log(
                    profile_id=profile.id,
                    response_time_ms=100,
                    created_at=created_at,
                    api_family="openai",
                ),
                _request_log(
                    profile_id=profile.id,
                    response_time_ms=110,
                    created_at=created_at,
                    api_family="anthropic",
                ),
            ]
        )
        await db.commit()

        items, total = await get_request_logs(
            db,
            profile_id=profile.id,
            api_family="openai",
            limit=50,
            offset=0,
        )

    assert total == 1
    assert [item.api_family for item in items] == ["openai"]


@pytest.mark.asyncio
async def test_get_stats_summary_groups_by_api_family() -> None:
    from app.services.stats.summary import get_stats_summary

    created_at = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)

    async with AsyncSessionLocal() as db:
        profile = Profile(
            name=f"summary-api-family-profile-{uuid4()}",
            is_active=False,
            is_default=False,
        )
        db.add(profile)
        await db.flush()

        db.add_all(
            [
                _request_log(
                    profile_id=profile.id,
                    response_time_ms=100,
                    created_at=created_at,
                    api_family="openai",
                ),
                _request_log(
                    profile_id=profile.id,
                    response_time_ms=110,
                    created_at=created_at,
                    api_family="openai",
                ),
                _request_log(
                    profile_id=profile.id,
                    response_time_ms=120,
                    created_at=created_at,
                    api_family="anthropic",
                ),
            ]
        )
        await db.commit()

        summary = await get_stats_summary(
            db,
            profile_id=profile.id,
            group_by="api_family",
        )

    summary_groups = summary["groups"]
    assert isinstance(summary_groups, list)
    assert {group["key"]: group["total_requests"] for group in summary_groups} == {
        "openai": 2,
        "anthropic": 1,
    }


@pytest.mark.asyncio
async def test_get_throughput_stats_filters_by_api_family() -> None:
    from app.services.stats.throughput import get_throughput_stats

    created_at = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)

    async with AsyncSessionLocal() as db:
        profile = Profile(
            name=f"throughput-api-family-profile-{uuid4()}",
            is_active=False,
            is_default=False,
        )
        db.add(profile)
        await db.flush()

        db.add_all(
            [
                _request_log(
                    profile_id=profile.id,
                    response_time_ms=100,
                    created_at=created_at,
                    api_family="openai",
                ),
                _request_log(
                    profile_id=profile.id,
                    response_time_ms=110,
                    created_at=created_at,
                    api_family="gemini",
                ),
            ]
        )
        await db.commit()

        report = await get_throughput_stats(
            db,
            profile_id=profile.id,
            api_family="openai",
        )

    assert report["total_requests"] == 1


@pytest.mark.asyncio
async def test_get_spending_report_groups_by_api_family() -> None:
    from app.services.stats.spending import get_spending_report

    created_at = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)

    async with AsyncSessionLocal() as db:
        profile = Profile(
            name=f"spending-api-family-profile-{uuid4()}",
            is_active=False,
            is_default=False,
        )
        db.add(profile)
        await db.flush()

        db.add_all(
            [
                _request_log(
                    profile_id=profile.id,
                    response_time_ms=100,
                    created_at=created_at,
                    api_family="openai",
                    success_flag=True,
                    billable_flag=True,
                    priced_flag=True,
                    total_cost_user_currency_micros=120,
                ),
                _request_log(
                    profile_id=profile.id,
                    response_time_ms=110,
                    created_at=created_at,
                    api_family="anthropic",
                    success_flag=True,
                    billable_flag=True,
                    priced_flag=True,
                    total_cost_user_currency_micros=80,
                ),
            ]
        )
        await db.commit()

        report = await get_spending_report(
            db,
            profile_id=profile.id,
            group_by="api_family",
        )

    report_groups = report["groups"]
    assert isinstance(report_groups, list)
    assert {group["key"]: group["total_cost_micros"] for group in report_groups} == {
        "openai": 120,
        "anthropic": 80,
    }
