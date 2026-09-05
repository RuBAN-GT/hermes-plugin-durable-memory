# Hermes Durable Memory

[![CI](https://github.com/RuBAN-GT/hermes-plugin-durable-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/RuBAN-GT/hermes-plugin-durable-memory/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/RuBAN-GT/hermes-plugin-durable-memory)](https://github.com/RuBAN-GT/hermes-plugin-durable-memory/releases)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![PostgreSQL](https://img.shields.io/badge/postgres-RLS%20ready-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

**A Hermes memory that survives sessions, stays inside one profile, and never
becomes searchable until a human approves it.**

```text
propose  ──►  pending  ──►  approve  ──►  searchable
                  │
                  └──►  reject
```

This is a generic Hermes Agent plugin. It stores typed records, searches them
later, and keeps private namespaces isolated unless you grant access.

| I want to… | Jump to |
| --- | --- |
| Try it in one minute | [Quick start](#quick-start) |
| Install into Hermes | [Install](#install) |
| Run durable PostgreSQL | [PostgreSQL](#postgresql) |
| Look up a command | [Commands](#commands) |
| Operate a deployment | [docs/operations.md](docs/operations.md) |

---

## Why this plugin

| You get | What that means |
| --- | --- |
| Durable recall | Facts live in PostgreSQL, not only in the current chat |
| Profile isolation | One Hermes profile cannot read another profile's private data |
| Approval-gated writes | Create, update, and delete stay pending until you approve them |
| Dynamic inventories | New record types in dialogue — no schema migration |
| Hybrid search | PostgreSQL FTS plus optional pgvector ranking, with FTS fallback |
| Human decisions | Approve from the CLI or Telegram buttons in the same session |
| Privacy controls | Export, retention windows, and two-admin hard purge |

---

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

1. Hermes talks to **one** external memory provider at a time.
2. This plugin registers commands, an AI tool, and the memory provider together.
3. Writes go through the approval policy. With `require`, nothing is searchable
   until a human decides.
4. PostgreSQL row-level security binds the runtime database role to one profile.

<details>
<summary>Optional skill extractors</summary>

Skills may act as candidate extractors. They receive only an explicit, bounded
`TurnContext` supplied by Hermes and return `MemoryCandidate` objects with
evidence. They are not memory writers: extractors do not receive a store,
approval queue, SQL connection, canonical records, or implicit chat history.

The default configuration has no extractors, so provider turn sync is a no-op.
When configured, the provider submits valid candidates through the same
approval policy as every other write. Turn context and raw dialogue are
transient and are never persisted automatically.

</details>

---

## Install

Install into the Python environment used by the Hermes CLI or gateway:

```bash
python3 -m pip install "git+https://github.com/RuBAN-GT/hermes-plugin-durable-memory.git"
```

Pin a release by appending the tag:

```bash
python3 -m pip install "git+https://github.com/RuBAN-GT/hermes-plugin-durable-memory.git@v0.1.0"
```

From a clone of this repository:

```bash
python3 -m pip install .
```

Restart Hermes, then select this plugin as the active memory provider:

```yaml
memory:
  provider: durable-memory
```

That selection enables cross-session recall. Commands and the AI tool come from
the same entry point.

> [!IMPORTANT]
> The plugin needs Hermes `register_tool`, `register_command`, and
> `register_memory_provider`. If that contract is missing, installation fails
> instead of silently disabling recall.

The only runtime dependency beyond Hermes is the PostgreSQL driver.

---

## Quick start

Smallest local setup: in-memory store. Useful for trying commands. Data does
**not** survive process restart.

```bash
export DURABLE_MEMORY_STORE=memory
export DURABLE_MEMORY_PROFILE=main
hermes durable-memory doctor
```

Then define a type, propose a record, approve it, and search:

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
[Configuration](#configuration) and [PostgreSQL](#postgresql).

---

## Status

Alpha (`0.1.0`). The PostgreSQL path is the production store.

| Area | Today |
| --- | --- |
| PostgreSQL repository | Versioned migrations through `0020_durability_guarantees` |
| Isolation | RLS binds a runtime role to one Hermes profile |
| Inventories | Dynamic records; typed fields, archetypes, and sensitivity |
| Search | Bounded FTS with optional vector/FTS rank fusion |
| Ollama | Optional `POST /api/embed` for projection indexing and query embedding |
| Vector / hybrid retrieval | pgvector projection with leased pending jobs; FTS remains the fallback |
| Telegram approval | Session-bound human decisions after capability consent |
| In-memory backend | Unit tests and local development only |
| Candidate conflicts | Exact identity is authoritative; optional semantic assessment stores duplicate/conflict metadata |
| Lifecycle | Validity windows retain records, revisions, candidates, and evidence after expiry |
| Privacy | Export, namespace retention, two-admin hard purge, resumable imports |

The Ollama adapter uses Python's standard library only. Embeddings are a
rebuildable projection, never canonical memory payloads. If Ollama is missing,
misconfigured, or unreachable, writes and FTS retrieval still work.

---

## Commands

```text
hermes durable-memory <action> [options]
/durable-memory <action> [options]
```

### Everyday

| Command | Purpose |
| --- | --- |
| `doctor` | Show backend, profile, and approval policy — never URLs |
| `search` | Bounded hybrid retrieval with FTS fallback |
| `propose` | Create, update, or delete through approval policy |
| `pending` | List change requests waiting for a human |
| `approve` / `reject` | Resolve a change request |

### Shape memory

| Command | Purpose |
| --- | --- |
| `namespaces` | List namespaces visible to this profile |
| `create-namespace` | Create a shared namespace |
| `grant` | Share `read`, `propose`, `approve`, or `admin` |
| `create-inventory` / `list-inventories` | Define or inspect dynamic types |

### Privacy

| Command | Purpose |
| --- | --- |
| `export` | Canonical user data without roles, URLs, or secrets |
| `set-retention` | Namespace validity window (`--seconds <n>` or `none`) |
| `request-hard-purge` | Ask a **different** namespace admin to erase a record |
| `approve-hard-purge` | Second-admin confirmation; this is the only path that deletes data |

### Operators

| Command | Purpose |
| --- | --- |
| `migrate` / `migration-status` | Apply or inspect versioned migrations |
| `bootstrap-profile` | Bind a profile slug to a PostgreSQL login role |

<details>
<summary>Search and update options</summary>

`search` accepts `--query`, `--type`, `--namespace`, `--filters` (JSON),
`--limit` (1–50, default 8), `--cursor`, `--sort`, and `--descending`. Schema
filters and sorting need an explicit namespace. Filters also need an explicit
type. Ranked text search does not support cursors without an explicit schema
sort.

Filter operators: `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `contains`.

For `propose --operation update`, the default `--replace false` applies the
payload as a merge patch and preserves omitted fields. Use `--replace true`
only when the submitted payload is the complete replacement; omitted fields
are removed. The structured API field is `replace` (boolean), persisted
internally as `update_mode` (`patch` or `replace`).

The AI tool cannot invoke approval, profile bootstrap, migrations, namespace
creation, grants, export, retention, or hard purge.

</details>

---

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

This flow does not change `ApprovalPolicy` and never enables auto-approval.

---

## Configuration

The process manager supplies environment variables. This plugin never reads
`.env` files and never prints database URLs, passwords, or other credentials.
See [`.env.example`](.env.example) for names and safe placeholders.

Profile comes from `DURABLE_MEMORY_PROFILE` or `$HERMES_PROFILE`. The two must
not conflict. Never infer another profile.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DURABLE_MEMORY_STORE` | required | `memory` or `postgres` |
| `DURABLE_MEMORY_PROFILE` | `$HERMES_PROFILE` | Runtime profile slug |
| `DURABLE_MEMORY_DATABASE_URL` | — | Runtime PostgreSQL URL |
| `DURABLE_MEMORY_MIGRATION_DATABASE_URL` | — | Migrations and bootstrap only |
| `DURABLE_MEMORY_APPROVAL_CREATE` | `require` | `require` \| `auto` \| `deny` |
| `DURABLE_MEMORY_APPROVAL_UPDATE` | `require` | `require` \| `auto` \| `deny` |
| `DURABLE_MEMORY_APPROVAL_DELETE` | `require` | `require` \| `auto` \| `deny` |
| `DURABLE_MEMORY_APPROVAL_TTL_SECONDS` | `86400` | Pending-request lifetime |

> [!WARNING]
> Use a separate `DURABLE_MEMORY_MIGRATION_DATABASE_URL` for migrations and
> profile bootstrap. Never run Hermes as the migration owner or a superuser.

Optional Ollama settings stay disabled unless every value is valid:

| Variable | Example |
| --- | --- |
| `DURABLE_MEMORY_EMBEDDING_PROVIDER` | `ollama` |
| `DURABLE_MEMORY_OLLAMA_BASE_URL` | `http://127.0.0.1:11434` |
| `DURABLE_MEMORY_OLLAMA_MODEL` | `nomic-embed-text` |
| `DURABLE_MEMORY_OLLAMA_TIMEOUT_SECONDS` | `10` |
| `DURABLE_MEMORY_EMBEDDING_MAX_DISTANCE` | `0.35` |

---

## Inventories

Inventories are typed schemas stored as ordinary approved records. Field kinds:

`string` · `text` · `integer` · `number` · `boolean` · `object` · `array` ·
`date` · `datetime` · `decimal` · `enum` · `reference` · `money` ·
`measurement`

Each field may be `required`, `filterable`, `searchable`, or `semantic`.
Inventories also carry `archetype` (`entity`, `event`, `observation`,
`relation`, `recommendation`, `collection_entry`) and `sensitivity`
(`normal`, `financial`, `health`).

---

## PostgreSQL

Production store. Start the local test database, then apply migrations:

```bash
docker compose -f docker-compose.test.yml up -d
export DURABLE_MEMORY_MIGRATION_DATABASE_URL='postgresql://durable_memory:durable_memory@127.0.0.1:55432/durable_memory_test'
hermes durable-memory migrate
hermes durable-memory migration-status
hermes durable-memory bootstrap-profile --slug main --runtime-role <postgres-role>
hermes durable-memory doctor
```

`doctor` must report an accepted `deployment_preflight`. It rejects a runtime
superuser, `BYPASSRLS`, schema/table ownership, missing RLS, dangerous
canonical-table or internal-function grants, missing checkpoint privileges,
required extensions/functions, or schema usage. It does not expose connection
URLs or credentials. For in-memory storage it reports not applicable.

After every migration, rerun `bootstrap-profile` as the migration owner for
each runtime profile. Set that profile's approval policy environment first.
Migration backfills are fail-safe (`require` with a 24-hour TTL); bootstrap
reconciles the database policy with the runtime configuration.

The migration owner needs a PostgreSQL deployment with the `vector` and
`pgcrypto` extensions available. Migration `0006_vector_projection.sql` runs
`CREATE EXTENSION vector`. Provision pgvector in managed PostgreSQL before
running it.

<details>
<summary>Runtime grants</summary>

Create separate migration-owner and profile-runtime roles. Run
`bootstrap-profile` as the migration owner, then grant the runtime role only
the application privileges it needs. Canonical writes go through
`submit_change_request(...)` and `decide_change_request(...)`.

```sql
GRANT USAGE ON SCHEMA durable_memory TO <runtime-role>;
GRANT SELECT ON durable_memory.profile TO <runtime-role>;
GRANT SELECT, INSERT, UPDATE ON durable_memory.namespace TO <runtime-role>;
GRANT SELECT, INSERT, DELETE ON durable_memory.namespace_grant TO <runtime-role>;
GRANT SELECT ON durable_memory.memory_type, durable_memory.memory_schema_version,
  durable_memory.inventory_definition TO <runtime-role>;
GRANT SELECT, INSERT ON durable_memory.memory_candidate TO <runtime-role>;
GRANT SELECT, INSERT ON durable_memory.memory_evidence TO <runtime-role>;
GRANT SELECT, INSERT ON durable_memory.candidate_record_relation TO <runtime-role>;
GRANT SELECT ON durable_memory.record, durable_memory.record_revision,
  durable_memory.change_request TO <runtime-role>;
GRANT EXECUTE ON FUNCTION durable_memory.submit_change_request(
  uuid, uuid, text, text, text, jsonb, text, integer, timestamptz, timestamptz,
  text, text)
  TO <runtime-role>;
GRANT EXECUTE ON FUNCTION durable_memory.decide_change_request(uuid, text)
  TO <runtime-role>;
GRANT EXECUTE ON FUNCTION durable_memory.proposal_record(uuid)
  TO <runtime-role>;
GRANT EXECUTE ON FUNCTION durable_memory.current_operation_policy()
  TO <runtime-role>;
GRANT EXECUTE ON FUNCTION durable_memory.save_import_checkpoint(
  text, text, text, jsonb) TO <runtime-role>;
GRANT EXECUTE ON FUNCTION durable_memory.load_import_checkpoint(text, text)
  TO <runtime-role>;
GRANT EXECUTE ON FUNCTION durable_memory.proposal_inventory_definition(uuid, text)
  TO <runtime-role>;
GRANT EXECUTE ON FUNCTION durable_memory.candidate_identity_assessment(
  uuid, text, text, jsonb, text) TO <runtime-role>;
GRANT EXECUTE ON FUNCTION durable_memory.consolidate_candidate(
  uuid, uuid, text, integer) TO <runtime-role>;
GRANT EXECUTE ON FUNCTION durable_memory.candidate_semantic_assessment(
  uuid, double precision, double precision) TO <runtime-role>;
GRANT EXECUTE ON FUNCTION durable_memory.set_namespace_retention(uuid, integer)
  TO <runtime-role>;
GRANT EXECUTE ON FUNCTION durable_memory.request_hard_purge(uuid, uuid, text)
  TO <runtime-role>;
GRANT EXECUTE ON FUNCTION durable_memory.approve_hard_purge(uuid)
  TO <runtime-role>;
```

Do **not** grant runtime roles `INSERT`, `UPDATE`, or `DELETE` on `record`,
`record_revision`, `change_request`, `memory_type`, or
`memory_schema_version`. Do not grant `apply_change_request(...)` or
`auto_apply_change_request(...)`. Embedding workers, retention jobs, and other
operator lifecycle actions require a separate operator role.

The migration does not grant application-table access to `PUBLIC`. PostgreSQL
RLS remains the data boundary after these grants.

</details>

<details>
<summary>Vector index, lifecycle, and workers</summary>

The projection supports multiple embedding models, so it does not create one
invalid HNSW index across mixed vector dimensions. After selecting a model and
dimension, the operator may add an appropriate partial cosine HNSW index:

```sql
CREATE INDEX record_embedding_nomic_hnsw ON durable_memory.record_embedding
USING hnsw ((embedding::vector(768)) vector_cosine_ops)
WHERE lifecycle_status = 'indexed' AND model_identifier = 'nomic-embed-text';
```

`valid_from` and `valid_to` are canonical record metadata, never payload
fields. New-record submissions through the typed `MemoryCandidate` API accept
timezone-aware validity metadata and carry it through their ordinary approval
request. Consolidating an existing candidate conflict and generic direct
proposals do not accept validity metadata yet.

Due records are excluded from full-text, vector, and semantic matching. They
are retained with their revision, candidate, and evidence history. Call the
bounded `DurableMemory.expire_records(limit=100)` service API to transition due
records to `expired`; it requires approval capability and never deletes data.

Projection workers are also service APIs, not CLI commands:

- `index_embeddings(limit=8)` — claim leased embedding jobs
- `assess_candidate_semantics(limit=8)` — candidate-only semantic jobs
- `requeue_embedding_jobs(limit=8)` — explicit retry of failed projections

Expired leases recover automatically. Exhausted leases stay failed until an
explicit requeue. Embeddings are never canonical payloads.

</details>

See [the operations runbook](docs/operations.md) for staged rollout, synthetic
smoke/load checks, embedding rollout, and rollback.

---

## Import

Candidate import is a Python API (`import_candidates`). Sources are read-only
and paged. Canonical writes still go through `submit_candidate` and therefore
through approval. Checkpoints are per profile, source, and scope.

The bundled `HolographicSQLiteSource` maps Hermes Holographic `facts` rows to
`holographic_fact` candidates. It opens the SQLite file read-only and never
creates or modifies source tables.

---

## Integrations

| Target | How |
| --- | --- |
| **OpenCode** | Copy [`integrations/opencode/durable-memory.ts`](integrations/opencode/durable-memory.ts) to `.opencode/plugins/durable-memory.ts`. Argument-safe `hermes durable-memory` execution. Does not modify user OpenCode configuration. |
| **Codex** | Use [`integrations/codex/AGENTS.md`](integrations/codex/AGENTS.md) as a project instruction template. |
| **Russian setup notes** | [`docs/end-to-end-ru.md`](docs/end-to-end-ru.md) |

---

## Tests

```bash
python3 -m unittest discover -s tests -v
ruff check .
ruff format --check .
```

Set `DURABLE_MEMORY_TEST_DATABASE_URL` to run PostgreSQL integration tests
against the compose database. Do not commit environment files, credentials,
database copies, or personal memory data.

---

## License

[MIT](LICENSE)
