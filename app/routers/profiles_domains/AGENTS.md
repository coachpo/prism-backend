# BACKEND PROFILES DOMAINS KNOWLEDGE BASE

## OVERVIEW
`profiles_domains/` is the profile-lifecycle package behind `../profiles.py`. It owns profile listing, active-profile lookup, create/update flows, activation, delete behavior, and helper rules around profile limits and name uniqueness.

## STRUCTURE
```
profiles_domains/
├── route_handlers.py   # List/get-active/create/update/activate/delete flows
└── helpers.py          # Name availability, active-profile locking, max-profile limit, shared loaders
```

## WHERE TO LOOK

- Profile lifecycle route handlers: `route_handlers.py`
- Name-availability checks, active-profile row locking, profile loading, and non-deleted profile limits: `helpers.py`

## CONVENTIONS

- Keep `profiles.py` thin and inject `ensure_profile_invariants()` from the service layer instead of recreating that logic here.
- Keep max-profile-count and name-availability rules in `helpers.py`.
- Keep active-profile lookup and update locking explicit through helper loaders.
- When doing upgrade work, backward compatibility with the pre-upgrade implementation is not a goal unless explicitly requested. Prefer the best current implementation shape over preserving the old one. Do not add compatibility shims, dual paths, or fallback behavior solely to preserve the old interface.

## ANTI-PATTERNS

- Do not bypass `ensure_profile_invariants()` on list/get-active/create/activate paths.
- Do not hardcode profile-count or duplicate-name rules outside `helpers.py`.
- Do not move profile lifecycle mutations back into `profiles.py`.
