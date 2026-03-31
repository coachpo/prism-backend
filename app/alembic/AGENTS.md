# BACKEND ALEMBIC KNOWLEDGE BASE

## OVERVIEW
`app/alembic/` is the packaged schema runtime for the backend. `env.py` drives offline and online execution, `script.py.mako` is the revision template, and `versions/` holds the checked-in schema source of truth. Prism currently ships one checked-in initial revision, and `app/core/migrations.py` is the programmatic seam used by startup code and tests.

## STRUCTURE
```
alembic/
├── env.py           # Offline/online migration runner wired to app.models.models metadata
├── script.py.mako   # Revision file template
└── versions/        # Checked-in schema revision(s) and schema source of truth
```

## WHERE TO LOOK

- Offline and online migration setup, including async runtime execution: `env.py`
- Revision file shape and naming template: `script.py.mako`
- Current schema revision and future schema source of truth: `versions/`
- Startup and test migration entrypoint seam: `../core/migrations.py`

## FACTS

- `env.py` imports `app.models.models` so metadata stays aligned with the ORM model boundary.
- `env.py` sets `target_metadata = Base.metadata` and supports both offline and online runs.
- `env.py` uses async migrations when no connection is provided by the caller.
- `../core/migrations.py` is the programmatic seam that startup code uses to run upgrades.
- `versions/0001_initial.py` is the current authoritative schema revision.
- `versions/` remains the schema source of truth, not ORM model state or startup side effects.

## CONVENTIONS

- Keep revision content in `versions/` and treat the checked-in initial revision as the authoritative schema install path.
- Keep runtime migration orchestration in `env.py` and the shared helper seam in `../core/migrations.py`.
- Keep this doc focused on the packaged Alembic surface, not backend schema design in general.
- When doing upgrade work, backward compatibility with the pre-upgrade implementation is not a goal unless explicitly requested. Do not add compatibility shims, dual paths, or fallback behavior solely to preserve the old interface.

## ANTI-PATTERNS

- Do not describe ORM models as the source of truth for schema state.
- Do not move migration execution details into startup code when `../core/migrations.py` already owns the seam.
- Do not add application feature logic to the Alembic package.
