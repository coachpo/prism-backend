# BACKEND PRICING TEMPLATES DOMAINS KNOWLEDGE BASE

## OVERVIEW
`pricing_templates_domains/` is the pricing-template route package behind `../pricing_templates.py`. It owns list/get/create/update/delete responses plus connection-usage lookups and CAS-safe update semantics for profile-scoped pricing templates.

## STRUCTURE
```
pricing_templates_domains/
├── route_handlers.py   # CRUD routes, CAS update, and usage lookup response
└── helpers.py          # Name uniqueness, connection usage rows, pricing-affecting field helpers
```

## WHERE TO LOOK

- CRUD routes, expected-updated-at CAS guard, version bumping, and usage lookup responses: `route_handlers.py`
- Name uniqueness checks, connection usage rows, and pricing-affecting field helpers: `helpers.py`

## CONVENTIONS

- Keep `pricing_templates.py` as a re-export shell; durable logic lives here.
- Keep expected-updated-at conflict handling and pricing-version bumps in `route_handlers.py`.
- Keep template-name uniqueness and connection-usage inspection in `helpers.py`.
- Treat pricing templates as selected-profile resources, not global instance settings.
- When doing upgrade work, backward compatibility with the pre-upgrade implementation is not a goal unless explicitly requested. Do not add compatibility shims, dual paths, or fallback behavior solely to preserve the old interface.

## ANTI-PATTERNS

- Do not bypass usage checks before deleting a template.
- Do not duplicate pricing-affecting field or version logic outside this package.
- Do not move pricing-template CRUD into model or connection router helpers.
