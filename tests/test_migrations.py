from __future__ import annotations

import json
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from hermes_durable_memory.config import Settings
from hermes_durable_memory.migrations import DatabaseMigrator
from hermes_durable_memory.models import CommandError, MemoryCandidate, MemoryEvidence
from hermes_durable_memory.policies import ApprovalPolicy
from hermes_durable_memory.service import DurableMemory
from hermes_durable_memory.store import PostgresStore

_DATABASE_URL = os.environ.get("DURABLE_MEMORY_TEST_DATABASE_URL")


def _role_url(role: str) -> str:
    if not _DATABASE_URL:
        raise RuntimeError("DURABLE_MEMORY_TEST_DATABASE_URL is not set")
    parsed = urlsplit(_DATABASE_URL)
    host = parsed.hostname or "localhost"
    port = f":{parsed.port}" if parsed.port else ""
    return f"postgresql://{role}:{role}-password@{host}{port}{parsed.path}"


@unittest.skipUnless(_DATABASE_URL, "DURABLE_MEMORY_TEST_DATABASE_URL is not set")
class PostgreSQLMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls.psycopg = psycopg
        cls.migrator = DatabaseMigrator(_DATABASE_URL or "")
        with psycopg.connect(_DATABASE_URL) as connection:
            connection.execute("DROP SCHEMA IF EXISTS durable_memory CASCADE")
            for role in ("durable_memory_alpha", "durable_memory_beta"):
                connection.execute(f"DROP ROLE IF EXISTS {role}")
                connection.execute(
                    f"CREATE ROLE {role} LOGIN PASSWORD '{role}-password'"
                )
        cls.migrator.migrate()
        cls.alpha = cls.migrator.bootstrap_profile("alpha", "durable_memory_alpha")
        cls.beta = cls.migrator.bootstrap_profile("beta", "durable_memory_beta")
        cls.namespace_id = str(uuid.uuid4())
        cls.record_id = str(uuid.uuid4())
        with psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "INSERT INTO durable_memory.operation_policy "
                "(profile_id, operation, action, ttl_seconds) VALUES (%s, 'create', "
                "'auto', 86400) ON CONFLICT (profile_id, operation) DO UPDATE "
                "SET action = EXCLUDED.action, ttl_seconds = EXCLUDED.ttl_seconds",
                (cls.alpha["id"],),
            )
            connection.execute(
                "GRANT USAGE ON SCHEMA durable_memory "
                "TO durable_memory_alpha, durable_memory_beta"
            )
            connection.execute(
                "GRANT SELECT ON durable_memory.profile "
                "TO durable_memory_alpha, durable_memory_beta"
            )
            connection.execute(
                "GRANT SELECT, INSERT, UPDATE ON durable_memory.namespace "
                "TO durable_memory_alpha, durable_memory_beta"
            )
            connection.execute(
                "GRANT SELECT, INSERT, DELETE ON durable_memory.namespace_grant "
                "TO durable_memory_alpha, durable_memory_beta"
            )
            connection.execute(
                "GRANT SELECT ON durable_memory.memory_type, "
                "durable_memory.memory_schema_version, "
                "durable_memory.inventory_definition "
                "TO durable_memory_alpha, durable_memory_beta"
            )
            connection.execute(
                "GRANT SELECT ON durable_memory.record, durable_memory.record_revision, "
                "durable_memory.change_request TO durable_memory_alpha, durable_memory_beta"
            )
            connection.execute(
                "GRANT SELECT, INSERT ON durable_memory.memory_candidate, "
                "durable_memory.memory_evidence, "
                "durable_memory.candidate_record_relation "
                "TO durable_memory_alpha, durable_memory_beta"
            )
            connection.execute(
                "GRANT SELECT, UPDATE ON durable_memory.candidate_embedding, "
                "durable_memory.candidate_embedding_job "
                "TO durable_memory_alpha, durable_memory_beta"
            )
            connection.execute(
                "GRANT SELECT, UPDATE ON durable_memory.record_embedding, "
                "durable_memory.embedding_job TO durable_memory_alpha, "
                "durable_memory_beta"
            )
            connection.execute(
                "GRANT EXECUTE ON FUNCTION "
                "durable_memory.decide_change_request(uuid, text) "
                "TO durable_memory_alpha, durable_memory_beta"
            )
            connection.execute(
                "GRANT EXECUTE ON FUNCTION "
                "durable_memory.proposal_inventory_definition(uuid, text) "
                "TO durable_memory_alpha, durable_memory_beta"
            )
            connection.execute(
                "GRANT EXECUTE ON FUNCTION "
                "durable_memory.candidate_identity_assessment("
                "uuid, text, text, jsonb, text) "
                "TO durable_memory_alpha, durable_memory_beta"
            )
            connection.execute(
                "GRANT EXECUTE ON FUNCTION "
                "durable_memory.consolidate_candidate(uuid, uuid, text, integer) "
                "TO durable_memory_alpha, durable_memory_beta"
            )
            connection.execute(
                "GRANT EXECUTE ON FUNCTION "
                "durable_memory.candidate_semantic_assessment("
                "uuid, double precision, double precision) "
                "TO durable_memory_alpha, durable_memory_beta"
            )
            connection.execute(
                "GRANT EXECUTE ON FUNCTION durable_memory.expire_records(integer) "
                "TO durable_memory_alpha, durable_memory_beta"
            )
            connection.execute(
                "GRANT EXECUTE ON FUNCTION "
                "durable_memory.proposal_inventory_definition(uuid, text) "
                "TO durable_memory_alpha, durable_memory_beta"
            )
            connection.execute(
                "INSERT INTO durable_memory.namespace "
                "(id, slug, kind, owner_profile_id) VALUES (%s, 'profile:alpha', "
                "'private', %s)",
                (cls.namespace_id, cls.alpha["id"]),
            )
            connection.execute(
                "INSERT INTO durable_memory.record "
                "(id, namespace_id, record_type, identity_key, status, revision, "
                "search_text, payload, created_by_profile_id, updated_by_profile_id) "
                "VALUES (%s, %s, 'fact', 'user:name', 'active', 1, 'Name is Ada', "
                "%s::jsonb, %s, %s)",
                (
                    cls.record_id,
                    cls.namespace_id,
                    json.dumps({"identity": "user:name", "text": "Name is Ada"}),
                    cls.alpha["id"],
                    cls.alpha["id"],
                ),
            )

    @classmethod
    def tearDownClass(cls) -> None:
        with cls.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute("DROP SCHEMA IF EXISTS durable_memory CASCADE")
            connection.execute("DROP ROLE IF EXISTS durable_memory_alpha")
            connection.execute("DROP ROLE IF EXISTS durable_memory_beta")

    def _connect_as(self, role: str):
        return self.psycopg.connect(_role_url(role))

    def test_migrate_tracks_version_and_checksum(self) -> None:
        self.assertEqual(self.migrator.migrate(), [])
        status = self.migrator.status()
        self.assertEqual(
            status,
            [
                {"version": 1, "name": "init", "status": "applied"},
                {
                    "version": 2,
                    "name": "runtime_backend",
                    "status": "applied",
                },
                {
                    "version": 3,
                    "name": "approval_boundary",
                    "status": "applied",
                },
                {"version": 4, "name": "p0_hardening", "status": "applied"},
                {"version": 5, "name": "candidate_intake", "status": "applied"},
                {"version": 6, "name": "vector_projection", "status": "applied"},
                {"version": 7, "name": "candidate_assessment", "status": "applied"},
                {"version": 8, "name": "candidate_consolidation", "status": "applied"},
                {
                    "version": 9,
                    "name": "candidate_semantic_assessment",
                    "status": "applied",
                },
                {
                    "version": 10,
                    "name": "inventory_schema_registry",
                    "status": "applied",
                },
                {
                    "version": 11,
                    "name": "lifecycle_retention_preflight",
                    "status": "applied",
                },
                {"version": 12, "name": "hardening_fixes", "status": "applied"},
                {
                    "version": 13,
                    "name": "trusted_submission",
                    "status": "applied",
                },
                {
                    "version": 14,
                    "name": "extended_field_kinds",
                    "status": "applied",
                },
                {
                    "version": 15,
                    "name": "semantic_assessment_policy",
                    "status": "applied",
                },
                {
                    "version": 16,
                    "name": "typed_record_metadata",
                    "status": "applied",
                },
                {
                    "version": 17,
                    "name": "embedding_job_leases",
                    "status": "applied",
                },
                {
                    "version": 18,
                    "name": "privacy_retention_purge_import",
                    "status": "applied",
                },
                {
                    "version": 19,
                    "name": "runtime_grant_hardening",
                    "status": "applied",
                },
            ],
        )

    def test_postgres_filters_before_ranked_limit(self) -> None:
        """A matching record beyond an unfiltered ranked page remains visible."""
        record_type = f"task_{uuid.uuid4().hex}"
        type_id = str(uuid.uuid4())
        fields = {"priority": {"kind": "integer", "filterable": True}}
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "INSERT INTO durable_memory.memory_type "
                "(id, namespace_id, record_type, created_by_profile_id) "
                "VALUES (%s, %s, %s, %s)",
                (type_id, self.namespace_id, record_type, self.alpha["id"]),
            )
            connection.execute(
                "INSERT INTO durable_memory.memory_schema_version "
                "(memory_type_id, version, fields, created_by_profile_id) "
                "VALUES (%s, 1, %s::jsonb, %s)",
                (type_id, json.dumps(fields), self.alpha["id"]),
            )
            for priority in range(60):
                connection.execute(
                    "INSERT INTO durable_memory.record "
                    "(id, namespace_id, record_type, identity_key, status, revision, "
                    "search_text, payload, created_by_profile_id, updated_by_profile_id) "
                    "VALUES (%s, %s, %s, %s, 'active', 1, 'task', %s::jsonb, %s, %s)",
                    (
                        str(uuid.uuid4()),
                        self.namespace_id,
                        record_type,
                        f"task:{priority:03}",
                        json.dumps(
                            {"identity": f"task:{priority:03}", "priority": priority}
                        ),
                        self.alpha["id"],
                        self.alpha["id"],
                    ),
                )

        memory = DurableMemory(
            settings=Settings(
                store="postgres",
                profile="alpha",
                policy=ApprovalPolicy(),
                database_url=_role_url("durable_memory_alpha"),
            )
        )
        result = memory.search(
            "task",
            record_type=record_type,
            filters={"priority": {"gte": 59}},
            limit=1,
        )

        self.assertEqual([item["identity"] for item in result["records"]], ["task:059"])

    def test_migrator_rejects_unknown_installed_version(self) -> None:
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "INSERT INTO durable_memory.schema_migration "
                "(version, name, checksum) VALUES (999, 'future', 'test')"
            )
        with self.assertRaisesRegex(CommandError, "unknown to this binary"):
            self.migrator.migrate()
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "DELETE FROM durable_memory.schema_migration WHERE version = 999"
            )

    def test_semantic_assessment_keeps_exact_identity_authoritative(self) -> None:
        candidate_id = str(uuid.uuid4())
        with self._connect_as("durable_memory_alpha") as connection:
            connection.execute(
                "INSERT INTO durable_memory.memory_candidate "
                "(id, namespace_id, record_type, identity_key, payload, text, "
                "canonical_payload, canonical_search_text, assessment, "
                "submitted_by_profile_id) "
                "VALUES (%s, %s, 'fact', 'user:name', %s::jsonb, 'Different name', "
                "%s::jsonb, 'Different name', 'new', %s)",
                (
                    candidate_id,
                    self.namespace_id,
                    json.dumps({"name": "Different"}),
                    json.dumps({"identity": "user:name", "name": "Different"}),
                    self.alpha["id"],
                ),
            )
            row = connection.execute(
                "SELECT record_id::text, assessment, reason FROM "
                "durable_memory.candidate_semantic_assessment(%s)",
                (candidate_id,),
            ).fetchone()
        self.assertEqual(
            row,
            (self.record_id, "conflict", "exact_identity_with_different_content"),
        )

    def test_approval_cannot_mutate_record_in_another_namespace(self) -> None:
        victim_id = str(uuid.uuid4())
        victim_namespace_id = str(uuid.uuid4())
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "INSERT INTO durable_memory.namespace "
                "(id, slug, kind, owner_profile_id) VALUES (%s, 'profile:beta', "
                "'private', %s)",
                (victim_namespace_id, self.beta["id"]),
            )
            connection.execute(
                "INSERT INTO durable_memory.record "
                "(id, namespace_id, record_type, identity_key, status, revision, "
                "search_text, payload, created_by_profile_id, updated_by_profile_id) "
                "VALUES (%s, %s, 'fact', 'victim:record', 'active', 1, "
                "'private beta record', %s::jsonb, %s, %s)",
                (
                    victim_id,
                    victim_namespace_id,
                    json.dumps({"identity": "victim:record"}),
                    self.beta["id"],
                    self.beta["id"],
                ),
            )
        alpha = DurableMemory(
            settings=Settings(
                store="postgres",
                profile="alpha",
                policy=ApprovalPolicy(update="require"),
                database_url=_role_url("durable_memory_alpha"),
            )
        )
        alpha_profile = alpha.store().get_or_create_profile("alpha")
        private = alpha.store().get_or_create_private_namespace(alpha_profile)
        with self._connect_as("durable_memory_alpha") as connection:
            with self.assertRaises(self.psycopg.errors.RaiseException):
                connection.execute(
                    "SELECT durable_memory.submit_change_request(%s, %s, 'delete', "
                    "'fact', 'victim:record', '{}'::jsonb, '', 1, NULL, NULL, %s)",
                    (private.id, victim_id, str(uuid.uuid4())),
                )
        with self.psycopg.connect(_DATABASE_URL) as connection:
            status = connection.execute(
                "SELECT status FROM durable_memory.record WHERE id = %s", (victim_id,)
            ).fetchone()[0]
            connection.execute(
                "DELETE FROM durable_memory.record WHERE id = %s", (victim_id,)
            )
            connection.execute(
                "DELETE FROM durable_memory.namespace WHERE id = %s",
                (victim_namespace_id,),
            )
        self.assertEqual(status, "active")

    def test_expired_record_is_not_searchable_and_retention_keeps_data(self) -> None:
        memory = DurableMemory(
            settings=Settings(
                store="postgres",
                profile="alpha",
                policy=ApprovalPolicy(create="auto"),
                database_url=_role_url("durable_memory_alpha"),
            )
        )
        result = memory.submit_candidate(
            MemoryCandidate(
                record_type="fact",
                identity_key=f"expired:{uuid.uuid4()}",
                payload={"value": "retained"},
                text="retained expiry evidence",
                valid_from=datetime.now(timezone.utc) - timedelta(days=1),
                valid_to=datetime.now(timezone.utc) - timedelta(seconds=1),
                evidence=(
                    MemoryEvidence(
                        source_kind="test",
                        source_ref="postgres-expiry",
                        observed_at=datetime.now(timezone.utc),
                        confidence=1,
                    ),
                ),
            )
        )
        self.assertEqual(memory.search("retained expiry evidence")["records"], [])
        self.assertEqual(memory.expire_records(limit=1)["affected"], 1)
        with self._connect_as("durable_memory_alpha") as connection:
            row = connection.execute(
                "SELECT status, payload ->> 'value' FROM durable_memory.record "
                "WHERE id = %s",
                (result["record_id"],),
            ).fetchone()
            evidence = connection.execute(
                "SELECT count(*) FROM durable_memory.memory_evidence "
                "WHERE candidate_id = %s",
                (result["candidate_id"],),
            ).fetchone()[0]
        self.assertEqual(row, ("expired", "retained"))
        self.assertEqual(evidence, 1)

    def test_postgres_deployment_preflight_accepts_runtime_role(self) -> None:
        store = PostgresStore(_role_url("durable_memory_alpha"))
        self.assertTrue(store.deployment_preflight()["ok"])

    def test_postgres_deployment_preflight_rejects_bypass_rls(self) -> None:
        store = PostgresStore(_role_url("durable_memory_alpha"))
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute("ALTER ROLE durable_memory_alpha BYPASSRLS")
        try:
            result = store.deployment_preflight()
            self.assertFalse(result["ok"])
            self.assertIn(
                "runtime role is superuser or has BYPASSRLS", result["checks"]
            )
        finally:
            with self.psycopg.connect(_DATABASE_URL) as connection:
                connection.execute("ALTER ROLE durable_memory_alpha NOBYPASSRLS")

    def test_postgres_deployment_preflight_rejects_canonical_write_grants(self) -> None:
        store = PostgresStore(_role_url("durable_memory_alpha"))
        protected_tables = (
            "record",
            "record_revision",
            "change_request",
            "memory_type",
            "memory_schema_version",
        )
        for table in protected_tables:
            with self.psycopg.connect(_DATABASE_URL) as connection:
                connection.execute(
                    f"GRANT INSERT ON durable_memory.{table} TO durable_memory_alpha"
                )
            try:
                result = store.deployment_preflight()
                self.assertFalse(result["ok"])
                self.assertIn(
                    f"runtime role has forbidden INSERT on {table}", result["checks"]
                )
            finally:
                with self.psycopg.connect(_DATABASE_URL) as connection:
                    connection.execute(
                        f"REVOKE INSERT ON durable_memory.{table} "
                        "FROM durable_memory_alpha"
                    )

    def test_postgres_deployment_preflight_rejects_auto_apply_grant(self) -> None:
        store = PostgresStore(_role_url("durable_memory_alpha"))
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "GRANT EXECUTE ON FUNCTION durable_memory.auto_apply_change_request(uuid) "
                "TO durable_memory_alpha"
            )
        try:
            result = store.deployment_preflight()
            self.assertFalse(result["ok"])
            self.assertIn(
                "runtime role has forbidden EXECUTE on auto_apply_change_request",
                result["checks"],
            )
        finally:
            with self.psycopg.connect(_DATABASE_URL) as connection:
                connection.execute(
                    "REVOKE EXECUTE ON FUNCTION "
                    "durable_memory.auto_apply_change_request(uuid) "
                    "FROM durable_memory_alpha"
                )

    def test_failed_record_embedding_job_can_be_explicitly_requeued(self) -> None:
        record_id = self.record_id
        with self._connect_as("durable_memory_alpha") as connection:
            connection.execute(
                "UPDATE durable_memory.record_embedding "
                "SET lifecycle_status = 'failed', "
                "error_message = 'provider unavailable' WHERE record_id = %s",
                (record_id,),
            )
            connection.execute(
                "UPDATE durable_memory.embedding_job SET status = 'failed', "
                "attempts = 2, "
                "last_error = 'provider unavailable', failed_at = now() "
                "WHERE record_id = %s AND revision = 1",
                (record_id,),
            )
        store = PostgresStore(_role_url("durable_memory_alpha"))
        self.assertEqual(
            store.requeue_failed_embedding_jobs(
                profile=store.get_or_create_profile("alpha"), limit=1
            ),
            1,
        )
        with self._connect_as("durable_memory_alpha") as connection:
            row = connection.execute(
                "SELECT job.status, job.attempts, job.last_error, "
                "projection.lifecycle_status, "
                "projection.error_message FROM durable_memory.embedding_job AS job "
                "JOIN durable_memory.record_embedding AS projection "
                "ON (projection.record_id, projection.revision) = "
                "(job.record_id, job.revision) WHERE job.record_id = %s",
                (record_id,),
            ).fetchone()
        self.assertEqual(
            row,
            ("pending", 2, "provider unavailable", "pending", "provider unavailable"),
        )

    def test_approve_and_propose_role_can_consolidate_candidate(self) -> None:
        namespace_id = str(uuid.uuid4())
        namespace = f"shared-{namespace_id}"
        identity = f"user:consolidation:{uuid.uuid4()}"
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "INSERT INTO durable_memory.namespace "
                "(id, slug, kind, owner_profile_id) VALUES (%s, %s, 'shared', %s)",
                (namespace_id, namespace, self.alpha["id"]),
            )
            connection.execute(
                "INSERT INTO durable_memory.namespace_grant "
                "(namespace_id, grantee_profile_id, capability, granted_by_profile_id) "
                "VALUES (%s, %s, 'approve', %s)",
                (namespace_id, self.beta["id"], self.alpha["id"]),
            )
            connection.execute(
                "INSERT INTO durable_memory.namespace_grant "
                "(namespace_id, grantee_profile_id, capability, granted_by_profile_id) "
                "VALUES (%s, %s, 'propose', %s)",
                (namespace_id, self.beta["id"], self.alpha["id"]),
            )
        alpha = DurableMemory(
            settings=Settings(
                store="postgres",
                profile="alpha",
                policy=ApprovalPolicy(create="auto", update="require"),
                database_url=_role_url("durable_memory_alpha"),
            )
        )
        alpha.execute_payload(
            f"propose --operation create --namespace {namespace} --identity {identity} "
            '--payload \'{"city":"Lisbon","country":"Portugal"}\' '
            "--text 'Lives in Lisbon'"
        )
        submitted = alpha.submit_candidate(
            MemoryCandidate(
                record_type="fact",
                identity_key=identity,
                namespace=namespace,
                payload={"city": "Porto", "metadata": {"source": "review"}},
                text="Lives in Porto",
                evidence=(
                    MemoryEvidence(
                        source_kind="test",
                        source_ref="candidate-consolidation",
                        observed_at=datetime.now(timezone.utc),
                        confidence=1,
                    ),
                ),
            )
        )
        beta = DurableMemory(
            settings=Settings(
                store="postgres",
                profile="beta",
                policy=ApprovalPolicy(update="require"),
                database_url=_role_url("durable_memory_beta"),
            )
        )
        consolidated = beta.consolidate_candidate(submitted["candidate_id"])
        self.assertEqual(consolidated["status"], "pending")
        self.assertEqual(consolidated["requested_by_profile_id"], self.beta["id"])
        self.assertEqual(consolidated["payload"]["city"], "Porto")
        self.assertNotIn("country", consolidated["payload"])
        self.assertEqual(consolidated["text"], "Lives in Porto")
        self.assertEqual(
            beta.consolidate_candidate(submitted["candidate_id"])["id"],
            consolidated["id"],
        )
        with self._connect_as("durable_memory_alpha") as connection:
            row = connection.execute(
                "SELECT record.revision, request.expected_revision, "
                "request.consolidated_candidate_id::text "
                "FROM durable_memory.record AS record "
                "JOIN durable_memory.change_request AS request "
                "ON request.record_id = record.id WHERE request.id = %s",
                (consolidated["id"],),
            ).fetchone()
        self.assertEqual(row, (1, 1, submitted["candidate_id"]))

    def test_runtime_store_persists_inventory_registry(self) -> None:
        memory = DurableMemory(
            settings=Settings(
                store="postgres",
                profile="alpha",
                policy=ApprovalPolicy(create="auto"),
                database_url=_role_url("durable_memory_alpha"),
            )
        )
        definition = memory.execute_payload(
            "create-inventory --type movie --fields "
            '\'{"title":{"kind":"string","required":true,'
            '"searchable":true},"rating":{"kind":"integer",'
            '"filterable":true}}\''
        )
        self.assertEqual(definition["status"], "approved")
        self.assertIsNone(definition["record_id"])
        with self._connect_as("durable_memory_alpha") as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM durable_memory.record "
                    "WHERE record_type = '__inventory_definition__' "
                    "AND identity_key = 'movie'"
                ).fetchone()[0],
                0,
            )
        created = memory.execute_payload(
            "propose --operation create --type movie --identity tmdb:438631 "
            '--payload \'{"title":"Dune: Part Two","rating":9}\''
        )
        self.assertEqual(created["status"], "approved")
        result = memory.execute_payload(
            'search --query Dune --type movie --filters \'{"rating":{"gte":8}}\''
        )
        self.assertEqual(
            [item["identity"] for item in result["records"]], ["tmdb:438631"]
        )

    def test_registry_migration_contains_legacy_definition_backfill(self) -> None:
        migration = next(
            item
            for item in self.migrator._migrations()  # noqa: SLF001
            if item.version == 10
        )
        self.assertIn("FROM durable_memory.record AS record", migration.sql)
        self.assertIn("record_type = '__inventory_definition__'", migration.sql)
        self.assertIn("DELETE FROM durable_memory.record_embedding", migration.sql)

    def test_legacy_definitions_are_excluded_from_search_and_embedding_jobs(
        self,
    ) -> None:
        legacy_id = str(uuid.uuid4())
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "INSERT INTO durable_memory.record "
                "(id, namespace_id, record_type, identity_key, status, revision, "
                "search_text, payload, created_by_profile_id, updated_by_profile_id) "
                "VALUES (%s, %s, '__inventory_definition__', 'legacy-hidden', "
                "'active', 1, 'legacy definition', %s::jsonb, %s, %s)",
                (
                    legacy_id,
                    self.namespace_id,
                    json.dumps({"identity": "legacy-hidden", "fields": {}}),
                    self.alpha["id"],
                    self.alpha["id"],
                ),
            )
        store = PostgresStore(_role_url("durable_memory_alpha"))
        alpha = store.get_or_create_profile("alpha")
        self.assertNotIn(
            legacy_id,
            [record.id for record in store.search(profile=alpha, query="legacy")],
        )
        self.assertNotIn(
            legacy_id,
            [
                job["record_id"]
                for job in store.pending_embedding_jobs(profile=alpha, limit=20)
            ],
        )

    def test_runtime_role_cannot_spoof_another_profile_with_a_custom_guc(self) -> None:
        with self._connect_as("durable_memory_alpha") as connection:
            alpha_count = connection.execute(
                "SELECT count(*) FROM durable_memory.record"
            ).fetchone()[0]
            self.assertGreaterEqual(alpha_count, 1)
        with self._connect_as("durable_memory_beta") as connection:
            connection.execute(
                "SELECT set_config('app.profile_id', %s, false)", (self.alpha["id"],)
            )
            beta_count = connection.execute(
                "SELECT count(*) FROM durable_memory.record"
            ).fetchone()[0]
            self.assertEqual(beta_count, 0)

    def test_runtime_role_cannot_write_records_outside_approval_function(self) -> None:
        with self._connect_as("durable_memory_alpha") as connection:
            with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                connection.execute(
                    "UPDATE durable_memory.record SET search_text = 'bypass' WHERE id = %s",
                    (self.record_id,),
                )
            connection.rollback()

    def test_runtime_role_cannot_insert_an_already_approved_request(self) -> None:
        with self._connect_as("durable_memory_alpha") as connection:
            with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                connection.execute(
                    "INSERT INTO durable_memory.change_request "
                    "(id, namespace_id, operation, record_type, identity_key, payload, "
                    "search_text, idempotency_key, status, policy_action, "
                    "requested_by_profile_id, expires_at) "
                    "VALUES (%s, %s, 'create', 'fact', 'user:bypass', %s::jsonb, "
                    "'bypass', %s, 'approved', 'require', %s, "
                    "now() + interval '1 day')",
                    (
                        str(uuid.uuid4()),
                        self.namespace_id,
                        json.dumps({"identity": "user:bypass", "text": "bypass"}),
                        str(uuid.uuid4()),
                        self.alpha["id"],
                    ),
                )
            connection.rollback()

    def test_runtime_role_cannot_forge_auto_change_request(self) -> None:
        with self._connect_as("durable_memory_alpha") as connection:
            with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                connection.execute(
                    "INSERT INTO durable_memory.change_request "
                    "(id, namespace_id, operation, record_type, identity_key, payload, "
                    "search_text, idempotency_key, status, policy_action, "
                    "requested_by_profile_id, expires_at) "
                    "VALUES (%s, %s, 'create', 'fact', 'user:forged-auto', "
                    "%s::jsonb, 'forged', %s, 'pending', 'auto', %s, "
                    "now() + interval '1 day')",
                    (
                        str(uuid.uuid4()),
                        self.namespace_id,
                        json.dumps({"identity": "user:forged-auto"}),
                        str(uuid.uuid4()),
                        self.alpha["id"],
                    ),
                )
            connection.rollback()

    def test_runtime_role_cannot_call_internal_apply_function(self) -> None:
        with self._connect_as("durable_memory_alpha") as connection:
            with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                connection.execute(
                    "SELECT durable_memory.apply_change_request(%s, 'approve', true)",
                    (str(uuid.uuid4()),),
                )
            connection.rollback()

    def test_propose_only_role_cannot_read_canonical_record_payload(self) -> None:
        namespace_id = str(uuid.uuid4())
        record_id = str(uuid.uuid4())
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "INSERT INTO durable_memory.namespace "
                "(id, slug, kind, owner_profile_id) VALUES (%s, %s, 'shared', %s)",
                (namespace_id, f"proposal-{namespace_id}", self.alpha["id"]),
            )
            connection.execute(
                "INSERT INTO durable_memory.namespace_grant "
                "(namespace_id, grantee_profile_id, capability, granted_by_profile_id) "
                "VALUES (%s, %s, 'propose', %s)",
                (namespace_id, self.beta["id"], self.alpha["id"]),
            )
            connection.execute(
                "INSERT INTO durable_memory.record "
                "(id, namespace_id, record_type, identity_key, status, revision, "
                "search_text, payload, created_by_profile_id, updated_by_profile_id) "
                "VALUES (%s, %s, 'fact', 'secret:record', 'active', 1, "
                "'canonical secret', %s::jsonb, %s, %s)",
                (
                    record_id,
                    namespace_id,
                    json.dumps({"identity": "secret:record", "secret": "hidden"}),
                    self.alpha["id"],
                    self.alpha["id"],
                ),
            )
        with self._connect_as("durable_memory_beta") as connection:
            request_id = connection.execute(
                "SELECT durable_memory.submit_change_request("
                "%s, %s, 'update', '', '', %s::jsonb, '', 1, NULL, NULL, %s)",
                (
                    namespace_id,
                    record_id,
                    json.dumps({"review": "only"}),
                    str(uuid.uuid4()),
                ),
            ).fetchone()[0]
            review_payload = connection.execute(
                "SELECT payload FROM durable_memory.change_request WHERE id = %s",
                (request_id,),
            ).fetchone()[0]
            visible = connection.execute(
                "SELECT payload FROM durable_memory.record WHERE id = %s", (record_id,)
            ).fetchall()
        self.assertEqual(review_payload, {"review": "only"})
        self.assertNotIn("canonical secret", json.dumps(review_payload))
        self.assertEqual(visible, [])

    def test_change_requests_follow_requester_and_approver_visibility(self) -> None:
        alpha = DurableMemory(
            settings=Settings(
                store="postgres",
                profile="alpha",
                policy=ApprovalPolicy(),
                database_url=_role_url("durable_memory_alpha"),
            )
        )
        request_id = alpha.execute_payload(
            "propose --operation create --identity user:city --payload "
            '\'{"text":"Lives in Lisbon"}\''
        )["id"]
        with self._connect_as("durable_memory_alpha") as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM durable_memory.change_request WHERE id = %s",
                    (request_id,),
                ).fetchone()[0],
                1,
            )
        with self._connect_as("durable_memory_beta") as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM durable_memory.change_request "
                    "WHERE namespace_id = %s",
                    (self.namespace_id,),
                ).fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
