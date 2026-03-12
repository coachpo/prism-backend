"""WebSocket connection manager for realtime updates.

Manages active WebSocket connections, profile-scoped subscriptions,
and event broadcasting.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class RealtimeConnection:
    """Represents an active WebSocket connection."""

    def __init__(self, websocket: WebSocket, connection_id: str):
        self.websocket = websocket
        self.connection_id = connection_id
        self.profile_id: int | None = None
        self.channel: str | None = None
        self.authenticated = False

    async def send_json(self, data: dict[str, Any]) -> None:
        """Send JSON message to client."""
        try:
            await self.websocket.send_json(data)
        except Exception:
            logger.exception(
                "Failed to send message to connection %s", self.connection_id
            )


class ConnectionManager:
    """Manages WebSocket connections and profile-scoped broadcasting."""

    def __init__(self):
        # connection_id -> RealtimeConnection
        self.connections: dict[str, RealtimeConnection] = {}
        # (profile_id, channel) -> set of connection_ids
        self.rooms: dict[tuple[int, str], set[str]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> str:
        """Accept a new WebSocket connection and return connection ID."""
        await websocket.accept()
        connection_id = str(uuid4())
        connection = RealtimeConnection(websocket, connection_id)

        async with self._lock:
            self.connections[connection_id] = connection

        logger.info("WebSocket connection established: %s", connection_id)
        return connection_id

    async def disconnect(self, connection_id: str) -> None:
        """Remove connection and clean up subscriptions."""
        async with self._lock:
            connection = self.connections.pop(connection_id, None)
            if not connection:
                return

            # Remove from all rooms
            if connection.profile_id and connection.channel:
                room_key = (connection.profile_id, connection.channel)
                if room_key in self.rooms:
                    self.rooms[room_key].discard(connection_id)
                    if not self.rooms[room_key]:
                        del self.rooms[room_key]

        logger.info("WebSocket connection closed: %s", connection_id)

    async def subscribe(
        self, connection_id: str, profile_id: int, channel: str
    ) -> bool:
        """Subscribe connection to a profile-scoped channel."""
        async with self._lock:
            connection = self.connections.get(connection_id)
            if not connection:
                return False

            # Unsubscribe from previous room if any
            if connection.profile_id and connection.channel:
                old_room_key = (connection.profile_id, connection.channel)
                if old_room_key in self.rooms:
                    self.rooms[old_room_key].discard(connection_id)
                    if not self.rooms[old_room_key]:
                        del self.rooms[old_room_key]

            # Subscribe to new room
            connection.profile_id = profile_id
            connection.channel = channel
            room_key = (profile_id, channel)

            if room_key not in self.rooms:
                self.rooms[room_key] = set()
            self.rooms[room_key].add(connection_id)

        logger.info(
            "Connection %s subscribed to profile=%d channel=%s",
            connection_id,
            profile_id,
            channel,
        )
        return True

    async def broadcast_to_profile(
        self, profile_id: int, channel: str, message: dict[str, Any]
    ) -> int:
        """Broadcast message to all connections subscribed to profile/channel."""
        room_key = (profile_id, channel)

        async with self._lock:
            connection_ids = self.rooms.get(room_key, set()).copy()

        if not connection_ids:
            return 0

        # Send to all connections in room
        tasks = []
        for conn_id in connection_ids:
            connection = self.connections.get(conn_id)
            if connection:
                tasks.append(connection.send_json(message))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        logger.debug(
            "Broadcasted to profile=%d channel=%s: %d connections",
            profile_id,
            channel,
            len(connection_ids),
        )
        return len(connection_ids)

    async def send_to_connection(
        self, connection_id: str, message: dict[str, Any]
    ) -> bool:
        """Send message to specific connection."""
        connection = self.connections.get(connection_id)
        if not connection:
            return False

        await connection.send_json(message)
        return True

    def get_connection(self, connection_id: str) -> RealtimeConnection | None:
        """Get connection by ID."""
        return self.connections.get(connection_id)

    def get_stats(self) -> dict[str, Any]:
        """Get connection manager statistics."""
        return {
            "total_connections": len(self.connections),
            "total_rooms": len(self.rooms),
            "rooms": {
                f"profile_{profile_id}_{channel}": len(conn_ids)
                for (profile_id, channel), conn_ids in self.rooms.items()
            },
        }


# Global connection manager instance
connection_manager = ConnectionManager()
