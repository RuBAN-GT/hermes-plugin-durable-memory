# Hermes Durable Memory

[![CI](https://github.com/RuBAN-GT/hermes-plugin-durable-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/RuBAN-GT/hermes-plugin-durable-memory/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/RuBAN-GT/hermes-plugin-durable-memory)](https://github.com/RuBAN-GT/hermes-plugin-durable-memory/releases)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![PostgreSQL](https://img.shields.io/badge/postgres-RLS%20ready-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

Give Hermes a memory that survives sessions, stays inside one profile, and
never writes a fact until a human approves it.

This is a generic Hermes Agent plugin. It stores typed records, searches them
later, and keeps private namespaces isolated unless you grant access.

```text
  propose ──► pending ──► approve ──► searchable
                 │
                 └──► reject
```

## Contents

- [Why this plugin](#why-this-plugin)
- [How it works](#how-it-works)
- [Install in Hermes](#install-in-hermes)
- [Quick start](#quick-start)
- [Status](#status)
- [Local installation](#local-installation)
- [Configuration](#configuration)
- [PostgreSQL setup](#postgresql-setup)
- [Commands](#commands)
- [Approvals](#approvals)
- [Integrations](#integrations)
- [Tests](#tests)
- [License](#license)

## Why this plugin

| You get | What that means |
| --- | --- |
| Durable recall | Facts live in PostgreSQL, not only in the current chat |
| Profile isolation | One Hermes profile cannot read another profile's private data |
| Approval-gated writes | Create, update, and delete stay pending until you approve them |
| Dynamic inventories | Define new record types in dialogue, without a schema migration |
| Safe search | Full-text search plus filters on declared JSON fields |
| Human decisions | Approve from the CLI or Telegram buttons in the same session |

`hermes-plugin-history-manager` is a read-only session archive. It is not this
plugin's writer or search backend.

## How it works

```mermaid
flowchart LR
  A[Hermes CLI / gateway] --> B[durable-memory plugin]
  B --> C[Commands, tool, memory provider]
  C --> D[Service]
  D --> E{Store}
  E -->|local tests| F[In-memory]
  E -->|production| G[PostgreSQL + RLS]
  D --> H[Approval queue]
  H -->|approve| G
  H -->|reject| I[No stored fact]
```

1. Hermes talks to one external memory provider at a time.
2. The plugin registers commands, an AI tool, and the memory provider together.
3. Writes go through the approval policy. With `require`, nothing is searchable
   until a human decides.
4. PostgreSQL row-level security binds the runtime database role to one profile.

## Install in Hermes

Install the plugin into the Python environment used by the Hermes CLI or
gateway:

```bash
python3 -m pip install "git+https://github.com/RuBAN-GT/hermes-plugin-durable-memory.git"
```

Pin a release by appending the tag:

```bash
python3 -m pip install "git+https://github.com/RuBAN-GT/hermes-plugin-durable-memory.git@v0.1.0"
```

Restart Hermes, then select this plugin as the active memory provider:

```yaml
memory:
  provider: durable-memory
```

Hermes activates one external memory provider at a time. That selection enables
cross-session recall. Commands and the AI tool come from the same entry point.

> The plugin needs Hermes `register_tool`, `register_command`, and
> `register_memory_provider`. If that contract is missing, installation fails
> instead of silently disabling recall.

## Quick start

Smallest local setup: in-memory store. Useful for trying commands. Data does
not survive process restart.

```bash
export DURABLE_MEMORY_STORE=memory
export DURABLE_MEMORY_PROFILE=main
hermes durable-memory doctor
```

Create a type, propose a record, then search it. With the default `require`
policy, approve the write first:

```bash
hermes durable-memory create-inventory \
  --type person \
  --fields '{"name":{"kind":"string","required":true,"searchable":true},"age":{"kind":"integer","filterable":true}}'

hermes durable-memory propose \
  --operation create \
  --type person \
  --identity person:ada \
  --payload '{"name":"Ada","age":37}'

hermes durable-memory pending
hermes durable-memory approve --request-id <request-id>
hermes durable-memory search --query Ada --type person --filters '{"age":{"gte":18}}'
```

For durable, cross-session memory, switch to PostgreSQL. See
[Configuration](#configuration) and [PostgreSQL setup](#postgresql-setup).

## Status

| Area | Today |
| --- | --- |
| PostgreSQL repository | Implemented, with versioned migrations |
| Isolation | RLS binds a runtime role to one Hermes profile |
| Inventories | Dynamic records; no schema migration to add a type |
| Search | PostgreSQL FTS on `search_text` and identity, plus JSON filters |
| Ollama | Optional `POST /api/embed` adapter; embeddings are not stored |
| Vector / hybrid retrieval | Not implemented |
| Telegram approval | Session-bound human decisions after capability consent |
| In-memory backend | Available for unit tests and local development |

The Ollama adapter uses Python's standard library only. If it is missing,
misconfigured, or unreachable, ordinary memory operations still work.

## Local installation

From a clone of this repository:

```bash
python3 -m pip install .
```

The only runtime dependency beyond Hermes is the PostgreSQL driver. Restart the
Hermes CLI or gateway after install.

## Configuration

The process manager supplies environment variables. This plugin never reads
`.env` files and never prints database URLs, passwords, or other credentials.
See [`.env.example`](.env.example) for names and safe placeholders.

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
profile bootstrap. Never run Hermes as the migration owner or a superuser.

Optional Ollama settings stay disabled unless every value is valid:

```dotenv
DURABLE_MEMORY_EMBEDDING_PROVIDER=ollama
DURABLE_MEMORY_OLLAMA_BASE_URL=http://127.0.0.1:11434
DURABLE_MEMORY_OLLAMA_MODEL=nomic-embed-text
DURABLE_MEMORY_OLLAMA_TIMEOUT_SECONDS=10
```

## PostgreSQL setup

Start the local test database, then apply migrations:

```bash
docker compose -f docker-compose.test.yml up -d
export DURABLE_MEMORY_MIGRATION_DATABASE_URL='postgresql://durable_memory:durable_memory@127.0.0.1:55432/durable_memory_test'
hermes durable-memory migrate
hermes durable-memory migration-status
```

For production, create separate migration-owner and profile-runtime roles. Run
`bootstrap-profile` as the migration owner, then grant the runtime role only
the application privileges it needs. Run this once per runtime role:

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
| `doctor` | Show active backend and approval policy, without URLs |
| `migrate` / `migration-status` | Apply or inspect versioned migrations |
| `bootstrap-profile` | Bind a profile slug to a PostgreSQL login role |
| `namespaces` / `create-namespace` / `grant` | Inspect and share namespaces |
| `create-inventory` / `list-inventories` | Define or inspect dynamic inventories |
| `search` | FTS search with type, namespace, and JSON filters |
| `propose` | Create, update, or delete through approval policy |
| `pending` / `approve` / `reject` | Review and resolve change requests |

## Approvals

With `require`, a mutation stays invisible until approved.

```mermaid
sequenceDiagram
  participant User
  participant Hermes
  participant Plugin
  participant Store
  User->>Hermes: Ask to remember a fact
  Hermes->>Plugin: propose create/update/delete
  Plugin->>Store: change request = pending
  alt Telegram session with consent
    Plugin->>User: Human decision buttons
    User->>Plugin: approve or reject
  else CLI
    User->>Plugin: approve --request-id
  end
  Plugin->>Store: fact becomes searchable, or stays unpublished
```

In Telegram, a proposal made in an existing gateway session can request an
actor-bound decision through `ctx.human_decisions`. Grant the plugin's
`gateway.human_decisions` capability through Hermes' standard consent flow to
enable the buttons. Only the actor from the originating session can decide, and
the decision is single-use.

If consent is missing, the platform is unsupported, delivery fails, the session
is stale, or the request times out, the request remains `pending`. Resolve it
with:

```text
/durable-memory approve --request-id <id>
/durable-memory reject --request-id <id>
```

The AI tool cannot invoke approval, profile bootstrap, migrations, namespace
creation, or grants. This flow does not change `ApprovalPolicy` and never
enables auto-approval.

## Integrations

**OpenCode.** Copy [`integrations/opencode/durable-memory.ts`](integrations/opencode/durable-memory.ts)
to `.opencode/plugins/durable-memory.ts` in a consuming project. It uses
OpenCode's custom tool API and runs `hermes durable-memory` with argument-safe
process execution. It does not modify user OpenCode configuration.

**Codex.** Use [`integrations/codex/AGENTS.md`](integrations/codex/AGENTS.md) as
a project instruction template. It requires the Hermes CLI/tool, approval
safety, and profile isolation.

**Russian setup notes.** See [`docs/end-to-end-ru.md`](docs/end-to-end-ru.md).

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
