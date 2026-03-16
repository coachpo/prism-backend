from datetime import datetime
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException


class TestLoadbalanceDeleteContract:
    @pytest.mark.asyncio
    async def test_delete_accepts_custom_retention_window(self):
        from app.routers.loadbalance import delete_loadbalance_events

        background_tasks = BackgroundTasks()

        with patch(
            "app.routers.loadbalance.delete_loadbalance_events_in_background",
            new_callable=AsyncMock,
        ) as mock_delete_loadbalance_events:
            response = await delete_loadbalance_events(
                background_tasks=background_tasks,
                profile_id=1,
                before=None,
                older_than_days=45,
                delete_all=False,
            )
            await background_tasks()

        assert response.accepted is True
        mock_delete_loadbalance_events.assert_awaited_once_with(
            profile_id=1,
            before=None,
            older_than_days=45,
            delete_all=False,
        )

    @pytest.mark.asyncio
    async def test_delete_normalizes_before_cutoff(self):
        from app.routers.loadbalance import delete_loadbalance_events

        background_tasks = BackgroundTasks()

        with patch(
            "app.routers.loadbalance.delete_loadbalance_events_in_background",
            new_callable=AsyncMock,
        ) as mock_delete_loadbalance_events:
            response = await delete_loadbalance_events(
                background_tasks=background_tasks,
                profile_id=1,
                before=datetime(2025, 1, 1),
                older_than_days=None,
                delete_all=False,
            )
            await background_tasks()

        assert response.accepted is True
        _, call_kwargs = cast(
            tuple[tuple[object, ...], dict[str, object]],
            mock_delete_loadbalance_events.await_args_list[0],
        )
        normalized_before = cast(datetime, call_kwargs["before"])
        assert normalized_before.tzinfo is not None

    @pytest.mark.asyncio
    async def test_delete_rejects_multiple_modes(self):
        from app.routers.loadbalance import delete_loadbalance_events

        background_tasks = BackgroundTasks()

        with pytest.raises(HTTPException) as exc_info:
            await delete_loadbalance_events(
                background_tasks=background_tasks,
                profile_id=1,
                before=datetime(2025, 1, 1),
                older_than_days=7,
                delete_all=False,
            )

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_delete_rejects_missing_mode(self):
        from app.routers.loadbalance import delete_loadbalance_events

        background_tasks = BackgroundTasks()

        with pytest.raises(HTTPException) as exc_info:
            await delete_loadbalance_events(
                background_tasks=background_tasks,
                profile_id=1,
                before=None,
                older_than_days=None,
                delete_all=False,
            )

        assert exc_info.value.status_code == 400
