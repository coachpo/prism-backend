# BACKEND COSTING SMOKE REGRESSION CLUSTER KNOWLEDGE BASE

## OVERVIEW
`test_costing_cases/` holds costing smoke regressions for token parsing, special-token rules, pricing-template CAS, and cache-cost checks.

## WHERE TO LOOK
- `missing_special_price_policy_and_special_token_guard_tests.py`
- `pricing_template_update_cas_tests.py`
- `token_usage_parsing_and_cache_creation_cost_tests.py`

## CONVENTIONS
- Keep this folder focused on costing smoke cases only.
- Keep parent aggregators in sync through `../test_costing.py` and `../../test_smoke_defect_regressions.py` when new costing-side DEF leaves land here.
- When doing upgrade work, backward compatibility with the pre-upgrade implementation is not a goal unless explicitly requested. Do not add compatibility shims, dual paths, or fallback behavior solely to preserve the old interface.

## ANTI-PATTERNS
- Do not move config, proxy, or startup regressions into this folder.
- Do not collapse pricing and token-usage regressions into vague filenames.
