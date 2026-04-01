# BACKEND MONITORING SERVICE KNOWLEDGE BASE

## OVERVIEW
`services/monitoring/` owns monitoring drill-down queries and probe execution behind `../monitoring_service.py`: overview, vendor, and model response shaping; manual and scheduled probes; and passive-outcome feedback into load-balance runtime state.

## STRUCTURE
```
monitoring/
├── __init__.py         # Re-export surface used by monitoring_service.py
├── probe_runner.py     # Manual/scheduled probe execution, leases, and upstream requests
├── queries.py          # Overview, vendor, and model drill-down response shaping
├── routing_feedback.py # Probe/passive outcomes -> EWMA, cooldown, recovery, and circuit updates
└── scheduler.py        # Due-probe selection, interval clamping, and background loop
```

## WHERE TO LOOK
- Public import surface: `__init__.py`, `../monitoring_service.py`
- Manual and scheduled probe execution, header blocklist use, and probe leases: `probe_runner.py`
- Monitoring overview, vendor drill-down, and model-detail payload shaping: `queries.py`
- Probe-result persistence plus runtime cooldown, recovery, and EWMA updates: `routing_feedback.py`
- Background monitoring cadence, due-probe selection, and scheduler lifecycle: `scheduler.py`, `../../main.py`
- Thin management router that consumes this package: `../../routers/monitoring.py`

## CONVENTIONS
- Keep `../monitoring_service.py` and `__init__.py` as the public re-export boundary for routers and startup wiring.
- Keep route shells thin; `../../routers/monitoring.py` should delegate to the monitoring service facade.
- Keep probe execution in `probe_runner.py`, and keep runtime-store mutations in `routing_feedback.py`.
- Keep per-profile monitoring cadence in `scheduler.py`, including settings-driven interval clamping and due-probe selection.
- When doing upgrade work, backward compatibility with the pre-upgrade implementation is not a goal unless explicitly requested. Prefer the best current implementation shape over preserving the old one. Do not add compatibility shims, dual paths, or fallback behavior solely to preserve the old interface.

## ANTI-PATTERNS
- Do not duplicate monitoring response shaping in routers when `queries.py` already owns that contract.
- Do not bypass probe lease acquisition and release when manual and scheduled probes share the same runner.
- Do not update runtime cooldown, EWMA, or circuit-state monitoring feedback outside `routing_feedback.py`.
