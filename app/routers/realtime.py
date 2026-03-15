"""WebSocket realtime router for dashboard updates."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import decode_access_token
from app.core.config import get_settings
from app.dependencies import get_db
from app.models.domains.identity import Profile
from app.services.auth_service import get_or_create_app_auth_settings
from app.services.realtime.connection_manager import connection_manager
from sqlalchemy import select

router = APIRouter(prefix="/api/realtime", tags=["realtime"])
logger = logging.getLogger(__name__)
SUPPORTED_REALTIME_CHANNELS = frozenset({"dashboard", "statistics"})


async def authenticate_websocket(
    websocket: WebSocket, cookie_name: str
) -> dict[str, Any] | None:
    """Authenticate WebSocket connection using cookie-based session."""
    access_token = websocket.cookies.get(cookie_name)
    if not access_token:
        return None

    try:
        return decode_access_token(access_token)
    except Exception:
        logger.info("WebSocket authentication failed")
        return None


async def get_profile_by_id(db: AsyncSession, profile_id: int) -> Profile | None:
    """Get profile by ID."""
    stmt = select(Profile).where(Profile.id == profile_id, Profile.deleted_at.is_(None))
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """WebSocket endpoint for profile-scoped realtime updates."""
    connection_id = await connection_manager.connect(websocket)
    connection = connection_manager.get_connection(connection_id)

    if not connection:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return

    try:
        settings = get_settings()
        settings_row = await get_or_create_app_auth_settings(db)
        auth_enabled = bool(settings_row.auth_enabled)
        auth_payload = await authenticate_websocket(
            websocket, settings.auth_cookie_name
        )

        if auth_enabled:
            if not auth_payload:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return

            try:
                subject_id = int(str(auth_payload.get("sub")))
                token_version = int(str(auth_payload.get("token_version")))
            except (TypeError, ValueError):
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return

            if (
                subject_id != settings_row.id
                or token_version != settings_row.token_version
            ):
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return

        connection.authenticated = True
        await connection.send_json(
            {
                "type": "authenticated",
                "username": settings_row.username,
            }
        )

        # Send initial heartbeat
        await connection.send_json({"type": "heartbeat"})

        # Message loop
        while True:
            data = await websocket.receive_json()
            message_type = data.get("type")

            if message_type == "subscribe":
                if auth_enabled and not connection.authenticated:
                    await connection.send_json(
                        {
                            "type": "error",
                            "message": "Authentication required",
                        }
                    )
                    continue

                profile_id = data.get("profile_id")
                channel = data.get("channel", "dashboard")

                if not profile_id:
                    await connection.send_json(
                        {
                            "type": "error",
                            "message": "profile_id required",
                        }
                    )
                    continue

                if channel not in SUPPORTED_REALTIME_CHANNELS:
                    await connection.send_json(
                        {
                            "type": "error",
                            "message": f"Unsupported channel: {channel}",
                        }
                    )
                    continue

                # Verify profile exists
                profile = await get_profile_by_id(db, profile_id)
                if not profile:
                    await connection.send_json(
                        {
                            "type": "error",
                            "message": f"Profile {profile_id} not found",
                        }
                    )
                    continue

                # Subscribe to channel
                success = await connection_manager.subscribe(
                    connection_id, profile_id, channel
                )

                if success:
                    await connection.send_json(
                        {
                            "type": "subscribed",
                            "profile_id": profile_id,
                            "channel": channel,
                        }
                    )
                else:
                    await connection.send_json(
                        {
                            "type": "error",
                            "message": "Subscription failed",
                        }
                    )

            elif message_type == "unsubscribe":
                await connection_manager.unsubscribe(connection_id)
                await connection.send_json({"type": "unsubscribed"})

            elif message_type == "unsubscribe_channel":
                channel = data.get("channel")

                if not isinstance(channel, str) or channel.strip() == "":
                    await connection.send_json(
                        {
                            "type": "error",
                            "message": "channel required",
                        }
                    )
                    continue

                success = await connection_manager.unsubscribe_channel(
                    connection_id, channel
                )
                if success:
                    await connection.send_json(
                        {
                            "type": "unsubscribed",
                            "channel": channel,
                        }
                    )
                else:
                    await connection.send_json(
                        {
                            "type": "error",
                            "message": "Channel unsubscribe failed",
                        }
                    )

            elif message_type == "pong":
                # Client responded to heartbeat
                pass

            elif message_type == "ping":
                # Client initiated ping
                await connection.send_json({"type": "pong"})

            else:
                await connection.send_json(
                    {
                        "type": "error",
                        "message": f"Unknown message type: {message_type}",
                    }
                )

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: %s", connection_id)
    except Exception:
        logger.exception("WebSocket error for connection %s", connection_id)
    finally:
        await connection_manager.disconnect(connection_id)


@router.get("/stats")
async def get_realtime_stats():
    """Get realtime connection statistics (for debugging)."""
    return connection_manager.get_stats()
