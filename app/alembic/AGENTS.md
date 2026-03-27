# BACKEND ALEMBIC KNOWLEDGE BASE

## OVERVIEW
`app/alembic/` is the packaged migration runtime for the backend. `env.py` drives offline and online migration execution, `script.py.mako` is the revision template, and `versions/` holds the revision history. Alembic revisions here are the schema source of truth, and `app/core/migrations.py` is the programmatic seam used by startup code and tests.

## STRUCTURE
```
alembic/
├── env.py           # Offline/online migration runner wired to app.models.models metadata
├── script.py.mako   # Revision file template
└── versions/        # Ordered revision history and schema source of truth
```

## WHERE TO LOOK

- Offline and online migration setup, including async runtime execution: `env.py`
- Revision file shape and naming template: `script.py.mako`
- Schema history and autogenerate output: `versions/`
- Startup and test migration entrypoint seam: `../core/migrations.py`

## FACTS

- `env.py` imports `app.models.models` so metadata stays aligned with the ORM model boundary.
- `env.py` sets `target_metadata = Base.metadata` and supports both offline and online runs.
- `env.py` uses async migrations when no connection is provided by the caller.
- `../core/migrations.py` is the programmatic seam that startup code uses to run upgrades.
- `versions/` is the authoritative schema history, not ORM model state or startup side effects.

## CONVENTIONS

- Keep revision content in `versions/` and treat each file as an immutable step in schema history.
- Keep runtime migration orchestration in `env.py` and the shared helper seam in `../core/migrations.py`.
- Keep this doc focused on the packaged Alembic surface, not backend schema design in general.

## ANTI-PATTERNS

- Do not describe ORM models as the source of truth for schema state.
- Do not move migration execution details into startup code when `../core/migrations.py` already owns the seam.
- Do not add application feature logic to the Alembic package.
