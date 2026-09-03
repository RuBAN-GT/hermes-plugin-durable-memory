# Hermes Durable Memory

Generic Hermes Agent plugin for durable, profile-isolated memory with
approval-gated writes, dynamic inventories, and PostgreSQL persistence.

Version: `0.1.0`

## Install in Hermes

Install the plugin directly from GitHub in the Python environment used by the
Hermes CLI or gateway:

```bash
python3 -m pip install "git+https://github.com/RuBAN-GT/hermes-plugin-durable-memory.git"
```

For a specific release, append the tag after `@`:

```bash
python3 -m pip install "git+https://github.com/RuBAN-GT/hermes-plugin-durable-memory.git@v0.1.0"
```

Restart the Hermes CLI or gateway after installation. Enable the provider in
the Hermes configuration:

```yaml
memory:
  provider: durable-memory
```

Then configure the process environment. The smallest local-development setup
uses the in-memory backend:

```bash
export DURABLE_MEMORY_STORE=memory
export DURABLE_MEMORY_PROFILE=main
hermes durable-memory doctor
```

Use PostgreSQL for durable, cross-session memory. See
[Configuration](#configuration) and [PostgreSQL Setup](#postgresql-setup) for
the required runtime role, migrations, and environment variables.

## Status

- Runtime PostgreSQL repository and versioned migrations are implemented.
- PostgreSQL RLS binds a runtime database role to one Hermes profile; private
  namespaces are not visible across profiles without explicit grants.
- Dynamic inventory definitions are records, so inventories can be created by
  dialogue/tool without a schema migration.
- PostgreSQL search currently uses FTS (`search_text` and identity) plus exact
  and comparison filters on declared JSON fields.
- The optional Ollama adapter calls `POST /api/embed` using Python stdlib only.
  It is ready for a later embedding projection, but embeddings are not stored.
- Hybrid/vector retrieval is not implemented: the current schema has no vector
  column and Ollama does not change retrieval behavior.
- Telegram inline approval is available through Hermes' session-bound human
  decision API after capability consent; CLI approval remains available.
- The memory backend remains useful for unit tests and local development.

`hermes-plugin-history-manager` is a read-only session archive, not this
plugin's writer or search backend.

## Local Installation

```bash
python3 -m pip install .
```

The plugin requires no integration-specific runtime dependency beyond its
existing PostgreSQL driver. Restart the Hermes CLI or gateway after install.
It requires Hermes' `register_tool`, `register_command`, and
`register_memory_provider` plugin APIs; installation fails rather than silently
disabling durable recall when that contract is absent.

Hermes activates one external memory provider at a time. This selection enables
cross-session recall; commands and the AI tool are registered by the same
provider entry point.

## Configuration

The process manager supplies environment variables. This plugin never reads
`.env` files and never prints database URLs, passwords, or other credentials.
See `.env.example` for names and safe placeholders.

```dotenv
DURABLE_MEMORY_STORE=postgres
DURABLE_MEMORY_PROFILE=main
DURABLE_MEMORY_DATABASE_URL=postgresql://<runtime-user>:<password>@<host>:5432/<database>
DURABLE_MEMORY_APPROVAL_CREATE=require
DURABLE_MEMORY_APPROVAL_UPDATE=require
DURABLE_MEMORY_APPROVAL_DELETE=require
DURABLE_MEMORY_APPROVAL_TTL_SECONDS=86400
```

Use a separate `DURABLE_MEMORY_MIGRATION_DATABASE_URL` only for migrations and
profile bootstrap. Never run Hermes using the migration owner or a superuser.

Optional Ollama configuration is disabled unless all relevant values are
valid:

```dotenv
DURABLE_MEMORY_EMBEDDING_PROVIDER=ollama
DURABLE_MEMORY_OLLAMA_BASE_URL=http://127.0.0.1:11434
DURABLE_MEMORY_OLLAMA_MODEL=nomic-embed-text
DURABLE_MEMORY_OLLAMA_TIMEOUT_SECONDS=10
```

An unavailable or malformed optional adapter fails closed and leaves ordinary
memory operations available.

## PostgreSQL Setup

Start the local test database:

```bash
docker compose -f docker-compose.test.yml up -d
export DURABLE_MEMORY_MIGRATION_DATABASE_URL='postgresql://durable_memory:durable_memory@127.0.0.1:55432/durable_memory_test'
hermes durable-memory migrate
hermes durable-memory migration-status
```

For production, create separate migration-owner and profile-runtime roles. Run
`bootstrap-profile` as the migration owner, then grant the runtime role only
the application privileges it needs. Run this as the migration owner once for
each runtime role, replacing `<runtime-role>`:

```sql
GRANT USAGE ON SCHEMA durable_memory TO <runtime-role>;
GRANT SELECT ON durable_memory.profile TO <runtime-role>;
GRANT SELECT, INSERT, UPDATE ON durable_memory.namespace TO <runtime-role>;
GRANT SELECT, INSERT, DELETE ON durable_memory.namespace_grant TO <runtime-role>;
GRANT SELECT, INSERT, UPDATE ON durable_memory.record TO <runtime-role>;
GRANT SELECT, INSERT ON durable_memory.record_revision TO <runtime-role>;
GRANT SELECT, INSERT, UPDATE ON durable_memory.change_request TO <runtime-role>;
GRANT EXECUTE ON FUNCTION durable_memory.decide_change_request(uuid, text)
  TO <runtime-role>;
```

The migration does not grant application-table access to `PUBLIC`. PostgreSQL
RLS remains the data boundary after these grants.

## Commands

```text
hermes durable-memory <action> [options]
/durable-memory <action> [options]
```

| Command | Purpose |
| --- | --- |
| `doctor` | Show active backend and approval policy without URLs |
| `migrate` / `migration-status` | Apply or inspect versioned migrations |
| `bootstrap-profile` | Bind a profile slug to a PostgreSQL login role |
| `namespaces` / `create-namespace` / `grant` | Inspect and share namespaces |
| `create-inventory` / `list-inventories` | Define or inspect dynamic inventories |
| `search` | FTS search with type, namespace, and JSON filters |
| `propose` | Create, update, or delete through approval policy |
| `pending` / `approve` / `reject` | Review and resolve change requests |

Example:

```text
hermes durable-memory create-inventory --type person --fields '{"name":{"kind":"string","required":true,"searchable":true},"age":{"kind":"integer","filterable":true}}'
hermes durable-memory propose --operation create --type person --identity person:ada --payload '{"name":"Ada","age":37}'
hermes durable-memory search --query Ada --type person --filters '{"age":{"gte":18}}'
```

With `require`, a mutation is invisible until approved. In Telegram, a proposal
made in an existing gateway session requests an actor-bound decision through
`ctx.human_decisions`; grant the plugin's `gateway.human_decisions` capability
through Hermes' standard consent flow to enable the buttons. Only the actor
from the originating session can decide, and the decision is single-use.

If consent is missing, the platform is unsupported, delivery fails, the session
is stale, or the request times out, the durable-memory request remains `pending`
and can be resolved with `/durable-memory approve --request-id <id>` or
`reject`. The AI tool still cannot invoke approval, profile bootstrap,
migrations, namespace creation, or grants directly. This interaction does not
change `ApprovalPolicy` and never enables auto-approval.

## Integrations

Copy `integrations/opencode/durable-memory.ts` to
`.opencode/plugins/durable-memory.ts` in a consuming project. It uses OpenCode's
custom tool API and invokes `hermes durable-memory` with argument-safe process
execution; it does not modify user OpenCode configuration.

Use `integrations/codex/AGENTS.md` as a Codex project instruction template. It
requires the Hermes CLI/tool, approval safety, and profile isolation.

The concise Russian setup guide is
[`docs/end-to-end-ru.md`](docs/end-to-end-ru.md).

## Tests

```bash
python3 -m unittest discover -s tests -v
ruff check .
ruff format --check .
```

Set `DURABLE_MEMORY_TEST_DATABASE_URL` to run PostgreSQL integration tests
against the compose database. Do not commit environment files, credentials,
database copies, or personal memory data.

## License

[MIT](LICENSE)
