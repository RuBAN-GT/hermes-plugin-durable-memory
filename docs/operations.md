# Operations Runbook

This runbook uses process-provided environment variables only. Do not put
database URLs, passwords, candidate payloads, or production query results in
shell history, tickets, or this repository.

## Staging rollout

1. Take a database backup using the platform's approved backup process.
2. Run migrations with the migration-owner environment, never a Hermes runtime
   role:

   ```bash
   hermes durable-memory migration-status
   hermes durable-memory migrate
   hermes durable-memory migration-status
   ```

3. Before starting Hermes, rerun `bootstrap-profile` as the migration owner for
   every runtime profile. Supply that profile's configured approval-policy
   environment variables. This step is mandatory after migration: migration
   backfills use fail-safe `require` policies and a 24-hour TTL, while bootstrap
   reconciles the database rows with the runtime configuration.

   ```bash
   hermes durable-memory bootstrap-profile \
     --slug "$DURABLE_MEMORY_PROFILE" \
     --runtime-role <postgres-role>
   ```

4. Grant each runtime role the exact function access documented in the README,
   including `current_operation_policy()`,
   `save_import_checkpoint(text, text, text, jsonb)`, and
   `load_import_checkpoint(text, text)`. Do not rely on `PUBLIC` grants.

5. Start Hermes with one runtime profile and run:

   ```bash
   hermes durable-memory doctor
   ```

   The `deployment_preflight` result must be accepted. Do not proceed if it
    reports a superuser, `BYPASSRLS`, schema/table ownership, missing RLS,
    extensions/functions, or direct canonical-table and internal-apply grants.
    Runtime roles may use only `submit_change_request(...)` and
    `decide_change_request(...)` for canonical changes.

6. Repeat `doctor` for every runtime profile. A profile must not be able to
   search a namespace without an explicit `read` grant.

## Smoke test

Use synthetic, non-personal values in an isolated staging namespace.

1. Create one typed schema and submit a candidate with evidence.
2. Confirm it is `pending` when the create policy is `require`.
3. Approve it and verify bounded search returns the record.
4. Submit an equal candidate and confirm `duplicate` with no new create
   request.
5. Submit a candidate with the same identity and different content; confirm
   `conflict`, then consolidate it. The resulting update must follow the update
   policy.
6. Grant a second profile only `propose`; confirm it can submit a candidate but
   cannot search the namespace or inspect canonical payloads.
7. Set an expired validity interval on a synthetic candidate, approve it, call
   the bounded expiration API, and confirm the record is absent from search
   while its revision/evidence history remains present.
8. Update a synthetic record once with the default patch mode and confirm
   omitted fields remain. Update it again with `--replace true` and confirm the
   submitted payload becomes the complete record payload.

## Embedding rollout

1. Keep FTS enabled as the fallback before enabling an embedding provider.
2. Configure one embedding model for staging and run the bounded indexing API.
3. Monitor failed projection jobs and requeue them explicitly after resolving
   the provider failure. Explicit requeue starts a fresh retry budget while
   retaining the last diagnostic. Expired leases are recovered automatically;
   exhausted leases remain failed until an explicit requeue. Do not delete
   canonical records to retry embeddings.
4. After the model and dimension are stable, create a partial HNSW index for
   that exact model and dimension as described in the README.
5. Compare FTS-only and hybrid results using synthetic queries before enabling
   the provider for more profiles.

## Load checks

Run against synthetic data only. Capture aggregate latency and queue counts,
not payloads:

- concurrent candidate submission in one namespace;
- concurrent approval and consolidation of the same candidate;
- bounded FTS and hybrid search at the intended `limit`;
- embedding job failure, requeue, and completion;
- RLS checks across private, read-only, propose-only, and approve-only roles.

## Rollback

Do not roll back individual schema migrations in place. If a rollout fails:

1. Stop the new runtime deployment.
2. Keep the database unchanged for investigation; canonical records, evidence,
   revisions, and candidates are audit data.
3. Disable the embedding provider to return retrieval to FTS fallback if the
   issue is projection-only.
4. Restore from the approved pre-migration backup only when the incident plan
   requires a full database recovery.

## Migration history

Production migrations are append-only. Before release, record the highest
`durable_memory.schema_migration` version and checksums from every persistent
environment. Never edit an applied migration; append a new version and verify
the upgrade from the last published package state.

## Explicit unsafe deployments

The default runbook above uses separate roles. An operator may explicitly set
`DURABLE_MEMORY_DANGER_ALLOW_UNSAFE_RUNTIME=true` for a single-profile deployment
with one schema-owner/runtime role. See the README for the exception's boundaries.
In this mode inspect both `deployment_preflight.checks` and `.warnings`; `ok`
does not certify database-enforced isolation. Missing objects, runtime permissions,
and policy mismatches remain blocking. Without a separate migration URL, operator
commands reuse the runtime URL. The flag does not grant privileges or auto-approve
writes. Restore a restricted runtime role before disabling the flag.

## Interactive operator setup

`hermes durable-memory setup` performs the same migration/bootstrap/grant checks
before saving the explicitly selected profile. Enable the plugin once to expose
its CLI, or use `python -m hermes_durable_memory.setup_cli` in Hermes' environment.
See the README questionnaire workflow and file-update behavior. The wizard keeps
operator passwords in memory only. For strict-mode later migrations, provide the
separate migration URL temporarily or rerun setup. Setup resets approval policy to
`require`; it is not a general-purpose policy editor or import command.
