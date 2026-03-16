from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestThroughputService:
    @pytest.mark.asyncio
    async def test_get_throughput_stats_returns_rpm_metrics(self):
        from app.services.stats.throughput import get_throughput_stats

        from_time = datetime(2026, 3, 16, 10, 0, 0)
        to_time = datetime(2026, 3, 16, 10, 10, 0)
        db = AsyncMock()
        execute_result = MagicMock()
        execute_result.all.return_value = [
            SimpleNamespace(
                bucket_time=datetime(2026, 3, 16, 10, 0, 0), request_count=2
            ),
            SimpleNamespace(
                bucket_time=datetime(2026, 3, 16, 10, 9, 0), request_count=4
            ),
        ]
        db.execute.return_value = execute_result

        result = await get_throughput_stats(
            db,
            profile_id=7,
            from_time=from_time,
            to_time=to_time,
        )

        assert result == {
            "average_rpm": 0.6,
            "peak_rpm": 4.0,
            "current_rpm": 4.0,
            "total_requests": 6,
            "time_window_seconds": 600.0,
            "buckets": [
                {
                    "timestamp": "2026-03-16T10:00:00",
                    "request_count": 2,
                    "rpm": 2.0,
                },
                {
                    "timestamp": "2026-03-16T10:09:00",
                    "request_count": 4,
                    "rpm": 4.0,
                },
            ],
        }

    @pytest.mark.asyncio
    async def test_get_throughput_stats_preserves_bounded_window_without_rows(self):
        from app.services.stats.throughput import get_throughput_stats

        from_time = datetime(2026, 3, 16, 10, 0, 0)
        to_time = datetime(2026, 3, 16, 11, 0, 0)
        db = AsyncMock()
        execute_result = MagicMock()
        execute_result.all.return_value = []
        db.execute.return_value = execute_result

        result = await get_throughput_stats(
            db,
            profile_id=7,
            from_time=from_time,
            to_time=to_time,
        )

        assert result == {
            "average_rpm": 0.0,
            "peak_rpm": 0.0,
            "current_rpm": 0.0,
            "total_requests": 0,
            "time_window_seconds": 3600.0,
            "buckets": [],
        }
