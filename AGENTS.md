# Engineering Instructions

## Scope

- Keep this plugin generic. Do not add user-specific workflows, storage paths,
  deployments, profile names, or host names to source code or documentation.
- The current Hermes profile is defined by the process environment. Use
  `DURABLE_MEMORY_PROFILE` or `$HERMES_PROFILE`, and never infer another profile.
- In containers, use only paths and environment variables available inside the
  Hermes container.

## Design

- Apply KISS and DRY. Keep transport-independent behavior in `service.py` and
  use `plugin.py` only as a Hermes adapter.
- Treat PostgreSQL as the future source of truth. The in-memory store exists
  for unit tests and local development only.
- Do not reuse History Manager as a memory writer, migrator, or search backend.
- Do not access Telegram adapter internals for buttons or callbacks. The public
  plugin API does not safely expose that boundary.
- Do not introduce runtime dependencies without a concrete compatibility or
  security need.
- Put user-facing command and Telegram text through `i18n.t()`. Keep command
  names and API field names in English.

## Security

- Never log, test, document, or commit real credentials, personal memory data,
  database copies, or environment files.
- Isolation must be enforced in the store: a profile cannot read another
  profile's private namespace without an explicit grant.
- Create, update, and delete must go through the approval queue unless policy
  is explicitly `auto`.
- Do not print database URLs or passwords.

## Quality

Before committing, run:

```bash
python -m unittest discover -s tests -v
ruff check .
ruff format --check .
```

Use Conventional Commits. Prefer concise imperative subjects, for example
`feat: add namespaced approval queue`.
