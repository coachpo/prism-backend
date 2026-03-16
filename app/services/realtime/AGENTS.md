# BACKEND REALTIME SERVICE KNOWLEDGE BASE

## OVERVIEW
`services/realtime/` owns WebSocket room state and profile/channel broadcast helpers behind `routers/realtime.py`.

## STRUCTURE
```
realtime/
└── connection_manager.py   # Connection registry, (profile_id, channel) rooms, broadcast helpers, stats
```

## WHERE TO LOOK

- Connection object and outbound JSON sends: `connection_manager.py`
- Connect/disconnect lifecycle and room cleanup: `connection_manager.py`
- Profile/channel subscribe-unsubscribe flow: `connection_manager.py`
- Broadcast fan-out for `dashboard.update`: `connection_manager.py`, `../stats/logging.py`
- WebSocket auth and profile existence checks: `../../routers/realtime.py`

## CONVENTIONS

- Treat `connection_manager` as the single source of truth for live WebSocket connections and rooms.
- Rooms are keyed by `(profile_id, channel)`; the router should authenticate and validate profile existence before subscribing.
- Keep broadcast payload shaping outside this package; `services/stats/logging.py` builds `dashboard.update`, then hands it to the manager.

## ANTI-PATTERNS

- Do not keep subscription state in routers or page-specific code when the manager already tracks rooms.
- Do not broadcast directly to raw sockets outside `connection_manager.py`.
- Do not let one connection stay subscribed to multiple profile scopes at once; new profile subscriptions should replace the old room state.
