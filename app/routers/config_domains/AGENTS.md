# BACKEND CONFIG DOMAINS KNOWLEDGE BASE

## OVERVIEW
`config_domains/` is the config-management route package behind `../config.py`. It owns config export/import orchestration and header-blocklist CRUD, while keeping validation, execution, and export assembly in separate modules.

## STRUCTURE
```
config_domains/
├── import_export.py   # `/export` and `/import` routes plus version gate
├── export_builder.py  # Config export payload assembly
├── import_validator.py # Import payload validation before execution
├── import_executor.py # Import execution and DB mutation flow
└── blocklist.py       # Header-blocklist CRUD routes and helpers
```

## WHERE TO LOOK

- Route entrypoints and import version gate: `import_export.py`
- Export payload assembly: `export_builder.py`
- Import payload validation: `import_validator.py`
- Import execution: `import_executor.py`
- Header-blocklist CRUD routes: `blocklist.py`

## CONVENTIONS

- Keep `config.py` composition-only. The real work belongs in this package.
- Validate import payloads before execution and keep the explicit version gate in `import_export.py`.
- Keep export payload assembly in `export_builder.py` instead of inlining JSON construction in routes.
- Keep header-blocklist CRUD separate from config import/export so profile-scoped rule management stays isolated.

## ANTI-PATTERNS

- Do not bypass `validate_import_payload()` before `execute_import_payload()`.
- Do not hand-build export payloads outside `export_builder.py`.
- Do not move header-blocklist CRUD back into `config.py`.
