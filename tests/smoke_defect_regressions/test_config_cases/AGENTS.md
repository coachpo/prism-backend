# BACKEND CONFIG SMOKE REGRESSION CLUSTER KNOWLEDGE BASE

## OVERVIEW
`test_config_cases/` holds config smoke regressions for export/import, schema validation, and user-settings seed coverage.

## WHERE TO LOOK
- `config_roundtrip_numeric_ids_and_system_rule_timestamp_tests.py`
- `user_settings_seed_and_config_schema_validation_tests.py`

## CONVENTIONS
- Keep this folder focused on config smoke cases only.
- Wire new config-side regressions through `../test_config.py` and `../../test_smoke_defect_regressions.py` when they belong in the top-level DEF corpus.
- When doing upgrade work, backward compatibility with the pre-upgrade implementation is not a goal unless explicitly requested. Do not add compatibility shims, dual paths, or fallback behavior solely to preserve the old interface.

## ANTI-PATTERNS
- Do not move costing, proxy, or startup regressions into this folder.
- Do not hide config-contract regressions behind vague file names.
