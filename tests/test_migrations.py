from __future__ import annotations

import json
import os
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
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
        cls.fixture_lock = psycopg.connect(_DATABASE_URL)
        cls.fixture_lock.execute(
            "SELECT pg_advisory_lock(hashtext('durable_memory.integration_fixture'))"
        )
        cls.migrator = DatabaseMigrator(_DATABASE_URL or "")
        with psycopg.connect(_DATABASE_URL) as connection:
            connection.execute("DROP SCHEMA IF EXISTS durable_memory CASCADE")
            for role in ("durable_memory_alpha", "durable_memory_beta"):
                connection.execute(f"DROP ROLE IF EXISTS {role}")
                connection.execute(
                    f"CREATE ROLE {role} LOGIN PASSWORD '{role}-password'"
                )
        cls.migrator.migrate()
        cls.alpha = cls.migrator.bootstrap_profile(
            "alpha", "durable_memory_alpha", ApprovalPolicy(create="auto")
        )
        cls.beta = cls.migrator.bootstrap_profile(
            "beta", "durable_memory_beta", ApprovalPolicy()
        )
        cls.namespace_id = str(uuid.uuid4())
        cls.record_id = str(uuid.uuid4())
        with psycopg.connect(_DATABASE_URL) as connection:
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
        cls.fixture_lock.execute(
            "SELECT pg_advisory_unlock(hashtext('durable_memory.integration_fixture'))"
        )
        cls.fixture_lock.close()

    def _connect_as(self, role: str):
        return self.psycopg.connect(_role_url(role))

    def _wait_for_lock_wait(self, application_name: str) -> None:
        with self.psycopg.connect(_DATABASE_URL) as connection:
            for _ in range(500):
                waiting = connection.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE application_name = %s AND state = 'active' "
                    "AND wait_event_type = 'Lock')",
                    (application_name,),
                ).fetchone()[0]
                if waiting:
                    return
                threading.Event().wait(0.01)
        self.fail(f"{application_name} did not reach a lock wait")

    def _wait_for_advisory_locks(
        self, application_name: str, expected: tuple[int, int]
    ) -> None:
        with self.psycopg.connect(_DATABASE_URL) as connection:
            for _ in range(500):
                counts = connection.execute(
                    "SELECT count(*) FILTER (WHERE lock.granted), "
                    "count(*) FILTER (WHERE NOT lock.granted) "
                    "FROM pg_locks AS lock JOIN pg_stat_activity AS activity "
                    "ON activity.pid = lock.pid WHERE lock.locktype = 'advisory' "
                    "AND activity.application_name = %s",
                    (application_name,),
                ).fetchone()
                if counts == expected:
                    return
                threading.Event().wait(0.01)
        self.fail(f"{application_name} did not reach advisory lock state {expected}")

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
                {
                    "version": 20,
                    "name": "durability_guarantees",
                    "status": "applied",
                },
            ],
        )

    def test_bootstrap_profile_reconciles_database_policy(self) -> None:
        # Given
        self.addCleanup(
            self.migrator.bootstrap_profile,
            "alpha",
            "durable_memory_alpha",
            ApprovalPolicy(create="auto"),
        )
        policy = ApprovalPolicy(
            create="deny", update="auto", delete="require", ttl_seconds=321
        )

        # When
        profile = self.migrator.bootstrap_profile(
            "alpha", "durable_memory_alpha", policy
        )

        # Then
        with self.psycopg.connect(_DATABASE_URL) as connection:
            rows = connection.execute(
                "SELECT operation, action, ttl_seconds "
                "FROM durable_memory.operation_policy WHERE profile_id = %s "
                "ORDER BY operation",
                (profile["id"],),
            ).fetchall()
        self.assertEqual(
            rows,
            [
                ("create", "deny", 321),
                ("delete", "require", 321),
                ("update", "auto", 321),
            ],
        )

    def test_bootstrap_profile_defaults_to_fail_safe_policy(self) -> None:
        # When
        profile = self.migrator.bootstrap_profile("beta", "durable_memory_beta")

        # Then
        with self.psycopg.connect(_DATABASE_URL) as connection:
            rows = connection.execute(
                "SELECT operation, action, ttl_seconds "
                "FROM durable_memory.operation_policy WHERE profile_id = %s "
                "ORDER BY operation",
                (profile["id"],),
            ).fetchall()
        self.assertEqual(
            rows,
            [
                ("create", "require", 86400),
                ("delete", "require", 86400),
                ("update", "require", 86400),
            ],
        )

    def test_doctor_reports_effective_policy_and_fails_on_mismatch(self) -> None:
        # Given
        memory = DurableMemory(
            settings=Settings(
                store="postgres",
                profile="alpha",
                policy=ApprovalPolicy(),
                database_url=_role_url("durable_memory_alpha"),
            )
        )

        # When
        result = memory.doctor()

        # Then
        self.assertEqual(
            (
                result["policy"],
                result["configured_policy"],
                result["policy_mismatch"],
                result["postgres_ready"],
            ),
            (
                ApprovalPolicy(create="auto").as_dict(),
                ApprovalPolicy().as_dict(),
                True,
                False,
            ),
        )
        self.assertTrue(
            any(
                "approval policy" in check
                for check in result["deployment_preflight"]["checks"]
            )
        )

    def test_postgres_mutation_fails_closed_on_policy_mismatch(self) -> None:
        # Given
        memory = DurableMemory(
            settings=Settings(
                store="postgres",
                profile="alpha",
                policy=ApprovalPolicy(),
                database_url=_role_url("durable_memory_alpha"),
            )
        )

        # When / Then
        with self.assertRaisesRegex(CommandError, "approval policy"):
            memory.execute_payload(
                "propose --operation create --identity policy:mismatch "
                '--payload \'{"value":"blocked"}\''
            )

    def test_create_fails_closed_when_only_update_policy_differs(self) -> None:
        # Given
        memory = DurableMemory(
            settings=Settings(
                store="postgres",
                profile="alpha",
                policy=ApprovalPolicy(create="auto", update="auto"),
                database_url=_role_url("durable_memory_alpha"),
            )
        )

        # When / Then
        with self.assertRaisesRegex(CommandError, "approval policy"):
            memory.execute_payload(
                f"propose --operation create --identity policy:{uuid.uuid4()} "
                '--payload \'{"value":"blocked"}\''
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
            namespace_slug="profile:alpha",
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
                    "'fact', 'victim:record', '{}'::jsonb, '', 1, NULL, NULL, "
                    "'patch', %s)",
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

    def test_danger_preflight_allows_owner_but_still_requires_rls(self) -> None:
        store = PostgresStore(_DATABASE_URL)
        self.assertFalse(store.deployment_preflight()["ok"])
        result = store.deployment_preflight(allow_unsafe_runtime=True)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["unsafe_runtime"])
        self.assertTrue(result["warnings"])
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "ALTER TABLE durable_memory.record DISABLE ROW LEVEL SECURITY"
            )
        try:
            result = store.deployment_preflight(allow_unsafe_runtime=True)
            self.assertFalse(result["ok"])
            self.assertIn("unsafe table configuration: record", result["checks"])
        finally:
            with self.psycopg.connect(_DATABASE_URL) as connection:
                connection.execute(
                    "ALTER TABLE durable_memory.record ENABLE ROW LEVEL SECURITY"
                )

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

    def test_postgres_deployment_preflight_rejects_profile_dml_authority(self) -> None:
        # Given
        store = PostgresStore(_role_url("durable_memory_alpha"))

        for privilege in ("INSERT", "UPDATE", "DELETE"):
            with self.subTest(privilege=privilege):
                with self.psycopg.connect(_DATABASE_URL) as connection:
                    connection.execute(
                        f"GRANT {privilege} ON durable_memory.profile "
                        "TO durable_memory_alpha"
                    )

                # When
                try:
                    result = store.deployment_preflight()
                finally:
                    with self.psycopg.connect(_DATABASE_URL) as connection:
                        connection.execute(
                            f"REVOKE {privilege} ON durable_memory.profile "
                            "FROM durable_memory_alpha"
                        )

                # Then
                self.assertFalse(result["ok"])
                self.assertIn(
                    f"runtime role has forbidden {privilege} on profile",
                    result["checks"],
                )

    def test_postgres_deployment_preflight_rejects_profile_ownership(self) -> None:
        # Given
        store = PostgresStore(_role_url("durable_memory_alpha"))
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "ALTER TABLE durable_memory.profile OWNER TO durable_memory_alpha"
            )

        # When
        try:
            result = store.deployment_preflight()
        finally:
            with self.psycopg.connect(_DATABASE_URL) as connection:
                connection.execute(
                    "ALTER TABLE durable_memory.profile OWNER TO CURRENT_USER"
                )
                connection.execute(
                    "GRANT SELECT ON durable_memory.profile TO durable_memory_alpha"
                )

        # Then
        self.assertFalse(result["ok"])
        self.assertIn("unsafe table configuration: profile", result["checks"])

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

    def test_postgres_deployment_preflight_requires_checkpoint_rls(self) -> None:
        # Given
        store = PostgresStore(_role_url("durable_memory_alpha"))
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "ALTER TABLE durable_memory.import_checkpoint DISABLE ROW LEVEL SECURITY"
            )

        # When
        try:
            result = store.deployment_preflight()
        finally:
            with self.psycopg.connect(_DATABASE_URL) as connection:
                connection.execute(
                    "ALTER TABLE durable_memory.import_checkpoint ENABLE ROW LEVEL SECURITY"
                )

        # Then
        self.assertFalse(result["ok"])
        self.assertIn("unsafe table configuration: import_checkpoint", result["checks"])

    def test_postgres_deployment_preflight_requires_checkpoint_functions(self) -> None:
        store = PostgresStore(_role_url("durable_memory_alpha"))
        functions = (
            (
                "save_import_checkpoint(text,text,text,jsonb)",
                "save_import_checkpoint EXECUTE",
            ),
            ("load_import_checkpoint(text,text)", "load_import_checkpoint EXECUTE"),
        )
        for signature, label in functions:
            with self.subTest(signature=signature):
                with self.psycopg.connect(_DATABASE_URL) as connection:
                    connection.execute(
                        f"REVOKE EXECUTE ON FUNCTION durable_memory.{signature} FROM PUBLIC"
                    )
                try:
                    result = store.deployment_preflight()
                    self.assertFalse(result["ok"])
                    self.assertIn(f"runtime role lacks {label}", result["checks"])
                finally:
                    with self.psycopg.connect(_DATABASE_URL) as connection:
                        connection.execute(
                            f"GRANT EXECUTE ON FUNCTION durable_memory.{signature} TO PUBLIC"
                        )

    def test_failed_record_embedding_job_can_be_explicitly_requeued(self) -> None:
        # Given
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
                "attempts = 3, max_attempts = 3, "
                "last_error = 'provider unavailable', failed_at = '-infinity'::timestamptz "
                "WHERE record_id = %s AND revision = 1",
                (record_id,),
            )
        store = PostgresStore(_role_url("durable_memory_alpha"))

        # When
        self.assertEqual(
            store.requeue_failed_embedding_jobs(
                profile=store.get_or_create_profile("alpha"), limit=1
            ),
            1,
        )

        # Then
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
            ("pending", 0, "provider unavailable", "pending", "provider unavailable"),
        )

    def test_candidate_embedding_recovery_respects_leases_and_retry_budgets(
        self,
    ) -> None:
        # Given
        candidate_ids = [str(uuid.uuid4()) for _ in range(6)]
        with self.psycopg.connect(_DATABASE_URL) as connection:
            for index, candidate_id in enumerate(candidate_ids):
                identity = f"candidate-lease:{candidate_id}"
                connection.execute(
                    "INSERT INTO durable_memory.memory_candidate "
                    "(id, namespace_id, record_type, identity_key, payload, text, "
                    "submitted_by_profile_id, canonical_payload, canonical_search_text) "
                    "VALUES (%s, %s, 'fact', %s, %s::jsonb, %s, %s, %s::jsonb, %s)",
                    (
                        candidate_id,
                        self.namespace_id,
                        identity,
                        json.dumps({"identity": identity, "index": index}),
                        f"candidate lease {index}",
                        self.alpha["id"],
                        json.dumps({"identity": identity, "index": index}),
                        f"candidate lease {index}",
                    ),
                )
            connection.execute(
                "UPDATE durable_memory.candidate_embedding_job "
                "SET status = 'processing', attempts = 1, max_attempts = 3, "
                "claim_token = gen_random_uuid(), claimed_at = now() - interval '20 minutes', "
                "lease_expires_at = now() + interval '5 minutes' WHERE candidate_id = %s",
                (candidate_ids[0],),
            )
            connection.execute(
                "UPDATE durable_memory.candidate_embedding_job "
                "SET status = 'processing', attempts = 1, max_attempts = 3, "
                "claim_token = gen_random_uuid(), claimed_at = now() - interval '20 minutes', "
                "lease_expires_at = now() - interval '5 minutes' WHERE candidate_id = %s",
                (candidate_ids[1],),
            )
            connection.execute(
                "UPDATE durable_memory.candidate_embedding_job "
                "SET status = 'processing', attempts = 3, max_attempts = 3, "
                "claim_token = gen_random_uuid(), claimed_at = now() - interval '20 minutes', "
                "lease_expires_at = now() - interval '5 minutes' WHERE candidate_id = %s",
                (candidate_ids[2],),
            )
            connection.execute(
                "UPDATE durable_memory.candidate_embedding_job "
                "SET status = 'failed', attempts = 3, max_attempts = 3, "
                "last_error = 'provider unavailable', failed_at = now(), "
                "claim_token = NULL, claimed_at = NULL, lease_expires_at = NULL "
                "WHERE candidate_id = %s",
                (candidate_ids[3],),
            )
            for candidate_id, attempts in (
                (candidate_ids[4], 1),
                (candidate_ids[5], 3),
            ):
                connection.execute(
                    "UPDATE durable_memory.candidate_embedding_job "
                    "SET status = 'processing', attempts = %s, max_attempts = 3, "
                    "claim_token = gen_random_uuid(), claimed_at = now(), "
                    "lease_expires_at = NULL WHERE candidate_id = %s",
                    (attempts, candidate_id),
                )
            connection.execute(
                "UPDATE durable_memory.candidate_embedding "
                "SET lifecycle_status = 'failed', error_message = 'provider unavailable', "
                "failed_at = now() WHERE candidate_id = %s",
                (candidate_ids[3],),
            )
            expected_requeued = connection.execute(
                "SELECT count(*) FROM durable_memory.candidate_embedding_job AS job "
                "JOIN durable_memory.memory_candidate AS candidate "
                "ON candidate.id = job.candidate_id "
                "WHERE candidate.submitted_by_profile_id = %s "
                "AND candidate.assessment = 'new' "
                "AND (job.status = 'failed' OR (job.status = 'processing' "
                "AND (job.lease_expires_at IS NULL OR job.lease_expires_at < now()) "
                "AND job.attempts < job.max_attempts))",
                (self.alpha["id"],),
            ).fetchone()[0]
        store = PostgresStore(_role_url("durable_memory_alpha"))

        # When
        affected = store.requeue_failed_candidate_embedding_jobs(
            profile=store.get_or_create_profile("alpha"), limit=10
        )

        # Then
        with self.psycopg.connect(_DATABASE_URL) as connection:
            rows = connection.execute(
                "SELECT job.candidate_id::text, job.status, job.attempts, job.last_error, "
                "job.claim_token IS NULL, job.lease_expires_at IS NULL, "
                "projection.lifecycle_status, projection.error_message "
                "FROM durable_memory.candidate_embedding_job AS job "
                "JOIN durable_memory.candidate_embedding AS projection "
                "ON projection.candidate_id = job.candidate_id "
                "WHERE job.candidate_id = ANY(%s)",
                (candidate_ids,),
            ).fetchall()
        states = {row[0]: row[1:] for row in rows}
        self.assertEqual(affected, expected_requeued)
        self.assertEqual(states[candidate_ids[0]][:3], ("processing", 1, None))
        self.assertEqual(
            states[candidate_ids[1]],
            ("pending", 1, None, True, True, "pending", None),
        )
        self.assertEqual(
            states[candidate_ids[2]],
            (
                "failed",
                3,
                "candidate embedding lease expired",
                True,
                True,
                "failed",
                "candidate embedding lease expired",
            ),
        )
        self.assertEqual(
            states[candidate_ids[3]],
            (
                "pending",
                0,
                "provider unavailable",
                True,
                True,
                "pending",
                "provider unavailable",
            ),
        )
        self.assertEqual(
            states[candidate_ids[4]],
            ("pending", 1, None, True, True, "pending", None),
        )
        self.assertEqual(
            states[candidate_ids[5]],
            (
                "failed",
                3,
                "candidate embedding lease expired",
                True,
                True,
                "failed",
                "candidate embedding lease expired",
            ),
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
            "search --query Dune --namespace profile:alpha --type movie "
            '--filters \'{"rating":{"gte":8}}\''
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
                "%s, %s, 'update', '', '', %s::jsonb, '', 1, NULL, NULL, "
                "'patch', %s)",
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
                policy=ApprovalPolicy(create="auto"),
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

    def test_mutation_idempotency_includes_target_record(self) -> None:
        # Given
        other_record_id = str(uuid.uuid4())
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "INSERT INTO durable_memory.record "
                "(id, namespace_id, record_type, identity_key, status, revision, "
                "search_text, payload, created_by_profile_id, updated_by_profile_id) "
                "VALUES (%s, %s, 'fact', %s, 'active', 1, 'other target', "
                "%s::jsonb, %s, %s)",
                (
                    other_record_id,
                    self.namespace_id,
                    f"delete-target:{uuid.uuid4()}",
                    json.dumps({"value": "other"}),
                    self.alpha["id"],
                    self.alpha["id"],
                ),
            )
        store = PostgresStore(_role_url("durable_memory_alpha"))
        profile = store.get_or_create_profile("alpha")
        namespace = store.get_namespace("profile:alpha")

        # When
        first = store.propose(
            actor=profile,
            namespace=namespace,
            operation="delete",
            record_type="",
            identity_key="",
            search_text="",
            payload={},
            policy_action="require",
            ttl_seconds=86400,
            record_id=self.record_id,
        )
        second = store.propose(
            actor=profile,
            namespace=namespace,
            operation="delete",
            record_type="",
            identity_key="",
            search_text="",
            payload={},
            policy_action="require",
            ttl_seconds=86400,
            record_id=other_record_id,
        )

        # Then
        self.assertEqual(
            (first.id == second.id, first.record_id, second.record_id),
            (False, self.record_id, other_record_id),
        )

    def test_pending_updates_capture_base_revision(self) -> None:
        # Given
        record_id = str(uuid.uuid4())
        identity = f"revision-target:{uuid.uuid4()}"
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "INSERT INTO durable_memory.record "
                "(id, namespace_id, record_type, identity_key, status, revision, "
                "search_text, payload, created_by_profile_id, updated_by_profile_id) "
                "VALUES (%s, %s, 'fact', %s, 'active', 1, 'base revision', "
                "%s::jsonb, %s, %s)",
                (
                    record_id,
                    self.namespace_id,
                    identity,
                    json.dumps({"identity": identity, "value": "base"}),
                    self.alpha["id"],
                    self.alpha["id"],
                ),
            )
        store = PostgresStore(_role_url("durable_memory_alpha"))
        profile = store.get_or_create_profile("alpha")
        namespace = store.get_namespace("profile:alpha")
        first = store.propose(
            actor=profile,
            namespace=namespace,
            operation="update",
            record_type="",
            identity_key="",
            search_text="first update",
            payload={"value": "first"},
            policy_action="require",
            ttl_seconds=86400,
            record_id=record_id,
        )
        second = store.propose(
            actor=profile,
            namespace=namespace,
            operation="update",
            record_type="",
            identity_key="",
            search_text="second update",
            payload={"value": "second"},
            policy_action="require",
            ttl_seconds=86400,
            record_id=record_id,
        )

        # When
        first = store.decide(actor=profile, request_id=first.id, decision="approve")
        second = store.decide(actor=profile, request_id=second.id, decision="approve")

        # Then
        record = store.get_record(record_id)
        self.assertEqual(
            (
                first.expected_revision,
                second.expected_revision,
                first.status,
                second.status,
                record.revision,
                record.payload["value"],
            ),
            (1, 1, "approved", "superseded", 2, "first"),
        )

    def test_auto_update_retry_reuses_caller_revision_digest(self) -> None:
        # Given
        record_id = str(uuid.uuid4())
        identity = f"auto-retry:{uuid.uuid4()}"
        self.addCleanup(
            self.migrator.bootstrap_profile,
            "alpha",
            "durable_memory_alpha",
            ApprovalPolicy(create="auto"),
        )
        self.migrator.bootstrap_profile(
            "alpha",
            "durable_memory_alpha",
            ApprovalPolicy(create="auto", update="auto"),
        )
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "INSERT INTO durable_memory.record "
                "(id, namespace_id, record_type, identity_key, status, revision, "
                "search_text, payload, created_by_profile_id, updated_by_profile_id) "
                "VALUES (%s, %s, 'fact', %s, 'active', 1, 'before retry', "
                "%s::jsonb, %s, %s)",
                (
                    record_id,
                    self.namespace_id,
                    identity,
                    json.dumps({"identity": identity, "value": "before"}),
                    self.alpha["id"],
                    self.alpha["id"],
                ),
            )
        store = PostgresStore(_role_url("durable_memory_alpha"))
        profile = store.get_or_create_profile("alpha")
        namespace = store.get_namespace("profile:alpha")

        # When
        first = store.propose(
            actor=profile,
            namespace=namespace,
            operation="update",
            record_type="",
            identity_key="",
            search_text="after retry",
            payload={"value": "after"},
            policy_action="auto",
            ttl_seconds=86400,
            record_id=record_id,
        )
        second = store.propose(
            actor=profile,
            namespace=namespace,
            operation="update",
            record_type="",
            identity_key="",
            search_text="after retry",
            payload={"value": "after"},
            policy_action="auto",
            ttl_seconds=86400,
            record_id=record_id,
        )

        # Then
        record = store.get_record(record_id)
        self.assertEqual(
            (first.id, second.id, first.expected_revision, record.revision),
            (second.id, first.id, 1, 2),
        )

    def test_concurrent_auto_updates_finish_without_deadlock(self) -> None:
        # Given
        self.addCleanup(
            self.migrator.bootstrap_profile,
            "alpha",
            "durable_memory_alpha",
            ApprovalPolicy(create="auto"),
        )
        self.migrator.bootstrap_profile(
            "alpha",
            "durable_memory_alpha",
            ApprovalPolicy(create="auto", update="auto"),
        )
        record_id = str(uuid.uuid4())
        identity = f"concurrent-auto:{uuid.uuid4()}"
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "INSERT INTO durable_memory.record "
                "(id, namespace_id, record_type, identity_key, status, revision, "
                "search_text, payload, created_by_profile_id, updated_by_profile_id) "
                "VALUES (%s, %s, 'fact', %s, 'active', 1, 'before', %s::jsonb, %s, %s)",
                (
                    record_id,
                    self.namespace_id,
                    identity,
                    json.dumps({"identity": identity, "value": "before"}),
                    self.alpha["id"],
                    self.alpha["id"],
                ),
            )
        store = PostgresStore(_role_url("durable_memory_alpha"))
        profile = store.get_or_create_profile("alpha")
        namespace = store.get_namespace("profile:alpha")
        barrier = threading.Barrier(2)

        def submit(value: str):
            local_store = PostgresStore(_role_url("durable_memory_alpha"))
            barrier.wait(timeout=5)
            return local_store.propose(
                actor=profile,
                namespace=namespace,
                operation="update",
                record_type="",
                identity_key="",
                search_text=value,
                payload={"value": value},
                policy_action="auto",
                ttl_seconds=86400,
                record_id=record_id,
            )

        # When
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(submit, value) for value in ("first", "second")]
            requests = [future.result(timeout=10) for future in futures]

        # Then
        self.assertTrue(
            all(request.status in {"approved", "superseded"} for request in requests)
        )
        self.assertTrue(any(request.status == "approved" for request in requests))

    def test_server_idempotency_digest_separates_requesters(self) -> None:
        # Given
        namespace_id = str(uuid.uuid4())
        submission_key = str(uuid.uuid4())
        identity = f"requester-collision:{uuid.uuid4()}"
        self.addCleanup(
            self.migrator.bootstrap_profile,
            "alpha",
            "durable_memory_alpha",
            ApprovalPolicy(create="auto"),
        )
        self.migrator.bootstrap_profile(
            "alpha", "durable_memory_alpha", ApprovalPolicy()
        )
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "INSERT INTO durable_memory.namespace "
                "(id, slug, kind, owner_profile_id) VALUES (%s, %s, 'shared', %s)",
                (namespace_id, f"requester-collision-{uuid.uuid4()}", self.alpha["id"]),
            )
            connection.execute(
                "INSERT INTO durable_memory.namespace_grant "
                "(namespace_id, grantee_profile_id, capability, granted_by_profile_id) "
                "VALUES (%s, %s, 'propose', %s)",
                (namespace_id, self.beta["id"], self.alpha["id"]),
            )

        # When
        request_ids = []
        for role in ("durable_memory_alpha", "durable_memory_beta"):
            with self._connect_as(role) as connection:
                request_ids.append(
                    str(
                        connection.execute(
                            "SELECT durable_memory.submit_change_request("
                            "%s, NULL, 'create', 'fact', %s, %s::jsonb, '', NULL, "
                            "NULL, NULL, 'patch', %s)",
                            (
                                namespace_id,
                                identity,
                                json.dumps({"identity": identity}),
                                submission_key,
                            ),
                        ).fetchone()[0]
                    )
                )

        # Then
        with self.psycopg.connect(_DATABASE_URL) as connection:
            requesters = connection.execute(
                "SELECT requested_by_profile_id::text "
                "FROM durable_memory.change_request WHERE id = ANY(%s) ORDER BY 1",
                (request_ids,),
            ).fetchall()
        self.assertEqual(
            (len(set(request_ids)), {row[0] for row in requesters}),
            (2, {self.alpha["id"], self.beta["id"]}),
        )

    def test_server_idempotency_digest_covers_trusted_submission(self) -> None:
        # Given
        second_namespace_id = str(uuid.uuid4())
        submission_key = str(uuid.uuid4())
        identity = f"trusted-digest:{uuid.uuid4()}"
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "INSERT INTO durable_memory.namespace "
                "(id, slug, kind, owner_profile_id) VALUES (%s, %s, 'shared', %s)",
                (second_namespace_id, f"digest-{uuid.uuid4()}", self.alpha["id"]),
            )

        # When
        request_ids = []
        variants = (
            (self.namespace_id, {"identity": identity, "value": 1}, "one", None),
            (self.namespace_id, {"identity": identity, "value": 2}, "one", None),
            (self.namespace_id, {"identity": identity, "value": 1}, "two", None),
            (
                self.namespace_id,
                {"identity": identity, "value": 1},
                "one",
                datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            (second_namespace_id, {"identity": identity, "value": 1}, "one", None),
        )
        with self._connect_as("durable_memory_alpha") as connection:
            for namespace_id, payload, search_text, valid_from in variants:
                request_ids.append(
                    str(
                        connection.execute(
                            "SELECT durable_memory.submit_change_request("
                            "%s, NULL, 'create', 'fact', %s, %s::jsonb, %s, NULL, "
                            "%s, NULL, 'patch', %s)",
                            (
                                namespace_id,
                                identity,
                                json.dumps(payload),
                                search_text,
                                valid_from,
                                submission_key,
                            ),
                        ).fetchone()[0]
                    )
                )
            mutation_ids = [
                str(
                    connection.execute(
                        "SELECT durable_memory.submit_change_request("
                        "%s, %s, %s, '', '', %s::jsonb, '', NULL, "
                        "NULL, NULL, 'patch', %s)",
                        (
                            self.namespace_id,
                            self.record_id,
                            operation,
                            json.dumps({}),
                            submission_key,
                        ),
                    ).fetchone()[0]
                )
                for operation in ("update", "delete")
            ]

        # Then
        self.assertEqual(len(set(request_ids)), len(variants))
        self.assertEqual(len(set(mutation_ids)), 2)

    def test_runtime_profile_creates_private_namespace_on_first_use(self) -> None:
        # Given
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "DELETE FROM durable_memory.namespace_grant "
                "WHERE grantee_profile_id = %s",
                (self.beta["id"],),
            )
            connection.execute(
                "DELETE FROM durable_memory.namespace WHERE slug = 'profile:beta'"
            )
        memory = DurableMemory(
            settings=Settings(
                store="postgres",
                profile="beta",
                policy=ApprovalPolicy(),
                database_url=_role_url("durable_memory_beta"),
            )
        )

        # When
        result = memory.execute_payload("namespaces")

        # Then
        self.assertEqual(
            [
                (item["slug"], item["kind"], item["owner"])
                for item in result["namespaces"]
            ],
            [("profile:beta", "private", True)],
        )

    def test_private_namespace_creation_handles_insert_conflict(self) -> None:
        # Given
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "DELETE FROM durable_memory.namespace_grant "
                "WHERE grantee_profile_id = %s",
                (self.beta["id"],),
            )
            connection.execute(
                "DELETE FROM durable_memory.namespace WHERE slug = %s",
                ("profile:beta",),
            )
        store = PostgresStore(_role_url("durable_memory_beta"))
        profile = store.get_or_create_profile("beta")
        insert_started = threading.Event()

        class SignalingConnection:
            def __init__(self, connection) -> None:
                self.connection = connection

            def execute(self, query, params=None):
                if query.startswith("INSERT INTO durable_memory.namespace "):
                    insert_started.set()
                return self.connection.execute(query, params)

        @contextmanager
        def signaling_connection():
            with self.psycopg.connect(_role_url("durable_memory_beta")) as connection:
                yield SignalingConnection(connection)

        # When
        with self.psycopg.connect(_DATABASE_URL) as winning_connection:
            winning_connection.execute(
                "INSERT INTO durable_memory.namespace "
                "(id, slug, kind, owner_profile_id) VALUES (%s, %s, 'private', %s)",
                (str(uuid.uuid4()), "profile:beta", self.beta["id"]),
            )
            with (
                patch.object(store, "_connection", signaling_connection),
                ThreadPoolExecutor(max_workers=1) as executor,
            ):
                future = executor.submit(store.get_or_create_private_namespace, profile)
                self.assertTrue(insert_started.wait(timeout=5))
                winning_connection.commit()
                namespace = future.result(timeout=5)

        # Then
        self.assertEqual(
            (namespace.slug, namespace.kind, namespace.owner_profile_id),
            ("profile:beta", "private", self.beta["id"]),
        )

    def test_candidate_submission_is_atomic(self) -> None:
        # Given
        marker = f"evidence-failure:{uuid.uuid4()}"
        identity = f"atomic-candidate:{uuid.uuid4()}"
        function_name = f"reject_evidence_{uuid.uuid4().hex}"
        trigger_name = f"reject_evidence_{uuid.uuid4().hex}"
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                f"CREATE FUNCTION durable_memory.{function_name}() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN "
                f"IF NEW.source_ref = '{marker}' THEN "
                "RAISE EXCEPTION 'forced evidence failure'; END IF; RETURN NEW; END $$"
            )
            connection.execute(
                f"CREATE TRIGGER {trigger_name} BEFORE INSERT ON "
                "durable_memory.memory_evidence FOR EACH ROW EXECUTE FUNCTION "
                f"durable_memory.{function_name}()"
            )
        memory = DurableMemory(
            settings=Settings(
                store="postgres",
                profile="alpha",
                policy=ApprovalPolicy(create="auto"),
                database_url=_role_url("durable_memory_alpha"),
            )
        )

        try:
            # When
            with self.assertRaises(self.psycopg.errors.RaiseException):
                memory.submit_candidate(
                    MemoryCandidate(
                        record_type="fact",
                        identity_key=identity,
                        payload={"value": "must roll back"},
                        text="atomic candidate",
                        evidence=(
                            MemoryEvidence(
                                source_kind="test",
                                source_ref=marker,
                                observed_at=datetime.now(timezone.utc),
                                confidence=1,
                            ),
                        ),
                    )
                )

            # Then
            with self.psycopg.connect(_DATABASE_URL) as connection:
                counts = connection.execute(
                    "SELECT "
                    "(SELECT count(*) FROM durable_memory.memory_candidate "
                    "WHERE identity_key = %s), "
                    "(SELECT count(*) FROM durable_memory.change_request "
                    "WHERE identity_key = %s), "
                    "(SELECT count(*) FROM durable_memory.record "
                    "WHERE identity_key = %s), "
                    "(SELECT count(*) FROM durable_memory.embedding_job AS job "
                    "JOIN durable_memory.record AS record ON record.id = job.record_id "
                    "WHERE record.identity_key = %s)",
                    (identity, identity, identity, identity),
                ).fetchone()
            self.assertEqual(counts, (0, 0, 0, 0))
        finally:
            with self.psycopg.connect(_DATABASE_URL) as connection:
                connection.execute(
                    f"DROP TRIGGER IF EXISTS {trigger_name} ON "
                    "durable_memory.memory_evidence"
                )
                connection.execute(
                    f"DROP FUNCTION IF EXISTS durable_memory.{function_name}()"
                )

    def test_import_checkpoints_are_profile_scoped(self) -> None:
        # Given
        source = f"checkpoint-source:{uuid.uuid4()}"
        scope = f"checkpoint-scope:{uuid.uuid4()}"
        alpha_store = PostgresStore(_role_url("durable_memory_alpha"))
        beta_store = PostgresStore(_role_url("durable_memory_beta"))

        # When
        alpha_store.save_import_checkpoint(
            source=source,
            scope=scope,
            checkpoint="alpha-1",
            report={"profile": "alpha"},
        )
        beta_store.save_import_checkpoint(
            source=source,
            scope=scope,
            checkpoint="beta-1",
            report={"profile": "beta"},
        )

        # Then
        self.assertEqual(
            (
                alpha_store.load_import_checkpoint(source=source, scope=scope),
                beta_store.load_import_checkpoint(source=source, scope=scope),
            ),
            (
                {"checkpoint": "alpha-1", "report": {"profile": "alpha"}},
                {"checkpoint": "beta-1", "report": {"profile": "beta"}},
            ),
        )

    def test_hard_purge_removes_candidate_trace_without_fk_failure(self) -> None:
        # Given
        namespace_id = str(uuid.uuid4())
        namespace_slug = f"purge-{uuid.uuid4()}"
        identity = f"purge-candidate:{uuid.uuid4()}"
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "INSERT INTO durable_memory.namespace "
                "(id, slug, kind, owner_profile_id) VALUES (%s, %s, 'shared', %s)",
                (namespace_id, namespace_slug, self.alpha["id"]),
            )
            connection.execute(
                "INSERT INTO durable_memory.namespace_grant "
                "(namespace_id, grantee_profile_id, capability, granted_by_profile_id) "
                "VALUES (%s, %s, 'admin', %s)",
                (namespace_id, self.beta["id"], self.alpha["id"]),
            )
        alpha_memory = DurableMemory(
            settings=Settings(
                store="postgres",
                profile="alpha",
                policy=ApprovalPolicy(create="auto"),
                database_url=_role_url("durable_memory_alpha"),
            )
        )
        submitted = alpha_memory.submit_candidate(
            MemoryCandidate(
                record_type="fact",
                identity_key=identity,
                namespace=namespace_slug,
                payload={"value": "private content"},
                text="private purge content",
                evidence=(
                    MemoryEvidence(
                        source_kind="test",
                        source_ref=f"purge-evidence:{uuid.uuid4()}",
                        observed_at=datetime.now(timezone.utc),
                        confidence=1,
                    ),
                ),
            )
        )
        alpha_store = PostgresStore(_role_url("durable_memory_alpha"))
        beta_store = PostgresStore(_role_url("durable_memory_beta"))
        request = alpha_store.request_hard_purge(
            actor=alpha_store.get_or_create_profile("alpha"),
            namespace=alpha_store.get_namespace(namespace_slug),
            record_id=submitted["record_id"],
            reason="regression purge",
        )

        # When
        purged = beta_store.approve_hard_purge(
            actor=beta_store.get_or_create_profile("beta"), request_id=request["id"]
        )

        # Then
        with self.psycopg.connect(_DATABASE_URL) as connection:
            trail = connection.execute(
                "SELECT "
                "(SELECT count(*) FROM durable_memory.record WHERE id = %s), "
                "(SELECT count(*) FROM durable_memory.change_request WHERE id = %s), "
                "(SELECT count(*) FROM durable_memory.memory_candidate WHERE id = %s), "
                "(SELECT count(*) FROM durable_memory.memory_evidence WHERE candidate_id = %s), "
                "(SELECT count(*) FROM durable_memory.hard_purge_audit WHERE request_id = %s)",
                (
                    submitted["record_id"],
                    submitted["id"],
                    submitted["candidate_id"],
                    submitted["candidate_id"],
                    request["id"],
                ),
            ).fetchone()
        self.assertEqual((purged["status"], trail), ("purged", (0, 0, 0, 0, 1)))

    def test_change_approval_and_hard_purge_finish_without_deadlock(self) -> None:
        # Given
        namespace_id = str(uuid.uuid4())
        record_id = str(uuid.uuid4())
        change_request_id = str(uuid.uuid4())
        purge_request_id = str(uuid.uuid4())
        identity = f"approval-purge:{uuid.uuid4()}"
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "INSERT INTO durable_memory.namespace "
                "(id, slug, kind, owner_profile_id) VALUES (%s, %s, 'shared', %s)",
                (namespace_id, f"approval-purge-{uuid.uuid4()}", self.alpha["id"]),
            )
            for capability in ("approve", "admin"):
                connection.execute(
                    "INSERT INTO durable_memory.namespace_grant "
                    "(namespace_id, grantee_profile_id, capability, granted_by_profile_id) "
                    "VALUES (%s, %s, %s, %s)",
                    (namespace_id, self.beta["id"], capability, self.alpha["id"]),
                )
            connection.execute(
                "INSERT INTO durable_memory.record "
                "(id, namespace_id, record_type, identity_key, status, revision, "
                "search_text, payload, created_by_profile_id, updated_by_profile_id) "
                "VALUES (%s, %s, 'fact', %s, 'active', 1, 'before', %s::jsonb, %s, %s)",
                (
                    record_id,
                    namespace_id,
                    identity,
                    json.dumps({"identity": identity, "value": "before"}),
                    self.alpha["id"],
                    self.alpha["id"],
                ),
            )
            connection.execute(
                "INSERT INTO durable_memory.change_request "
                "(id, namespace_id, record_id, operation, record_type, identity_key, "
                "expected_revision, payload, search_text, idempotency_key, status, "
                "policy_action, requested_by_profile_id, expires_at) "
                "VALUES (%s, %s, %s, 'update', 'fact', %s, 1, %s::jsonb, 'after', "
                "%s, 'pending', 'require', %s, now() + interval '1 hour')",
                (
                    change_request_id,
                    namespace_id,
                    record_id,
                    identity,
                    json.dumps({"value": "after"}),
                    f"approval-purge-{uuid.uuid4()}",
                    self.alpha["id"],
                ),
            )
            connection.execute(
                "INSERT INTO durable_memory.change_request_private "
                "(request_id, payload, search_text) VALUES (%s, %s::jsonb, 'after')",
                (
                    change_request_id,
                    json.dumps({"identity": identity, "value": "after"}),
                ),
            )
            connection.execute(
                "INSERT INTO durable_memory.hard_purge_request "
                "(id, namespace_id, record_id, record_type, identity_key, "
                "requested_by_profile_id, reason) VALUES (%s, %s, %s, 'fact', %s, %s, %s)",
                (
                    purge_request_id,
                    namespace_id,
                    record_id,
                    identity,
                    self.alpha["id"],
                    "approval race regression",
                ),
            )
        purge_application = f"purge-{uuid.uuid4()}"
        approval_application = f"approval-{uuid.uuid4()}"
        purge_started = threading.Barrier(2)
        approval_started = threading.Barrier(2)

        def purge() -> str:
            with self._connect_as("durable_memory_beta") as connection:
                connection.execute("SET LOCAL statement_timeout = '5s'")
                connection.execute(
                    "SELECT set_config('application_name', %s, false)",
                    (purge_application,),
                )
                purge_started.wait(timeout=5)
                try:
                    connection.execute(
                        "SELECT durable_memory.approve_hard_purge(%s)",
                        (purge_request_id,),
                    ).fetchone()
                except self.psycopg.Error as error:
                    return error.sqlstate or "database-error"
            return "ok"

        def approve() -> str:
            with self._connect_as("durable_memory_beta") as connection:
                connection.execute("SET LOCAL statement_timeout = '5s'")
                connection.execute(
                    "SELECT set_config('application_name', %s, false)",
                    (approval_application,),
                )
                approval_started.wait(timeout=5)
                try:
                    connection.execute(
                        "SELECT durable_memory.decide_change_request(%s, 'approve')",
                        (change_request_id,),
                    ).fetchone()
                except self.psycopg.Error as error:
                    return error.sqlstate or "database-error"
            return "ok"

        # When
        with self.psycopg.connect(_DATABASE_URL) as blocker:
            blocker.execute(
                "SELECT id FROM durable_memory.record WHERE id = %s FOR UPDATE",
                (record_id,),
            ).fetchone()
            with ThreadPoolExecutor(max_workers=2) as executor:
                purge_future = executor.submit(purge)
                purge_started.wait(timeout=5)
                self._wait_for_lock_wait(purge_application)
                approval_future = executor.submit(approve)
                approval_started.wait(timeout=5)
                self._wait_for_lock_wait(approval_application)
                blocker.commit()
                outcomes = {
                    purge_future.result(timeout=8),
                    approval_future.result(timeout=8),
                }

        # Then
        self.assertNotIn("40P01", outcomes)
        self.assertLessEqual(outcomes, {"ok", "P0001"})
        with self.psycopg.connect(_DATABASE_URL) as connection:
            terminal = connection.execute(
                "SELECT "
                "(SELECT count(*) FROM durable_memory.record WHERE id = %s), "
                "(SELECT count(*) FROM durable_memory.change_request WHERE id = %s), "
                "(SELECT count(*) FROM durable_memory.hard_purge_audit WHERE request_id = %s)",
                (record_id, change_request_id, purge_request_id),
            ).fetchone()
        self.assertEqual(terminal, (0, 0, 1))

    def test_candidate_consolidation_and_hard_purge_finish_without_deadlock(
        self,
    ) -> None:
        # Given
        namespace_id = str(uuid.uuid4())
        record_id = str(uuid.uuid4())
        candidate_id = str(uuid.uuid4())
        purge_request_id = str(uuid.uuid4())
        identity = f"consolidation-purge:{uuid.uuid4()}"
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "INSERT INTO durable_memory.namespace "
                "(id, slug, kind, owner_profile_id) VALUES (%s, %s, 'shared', %s)",
                (namespace_id, f"consolidation-purge-{uuid.uuid4()}", self.alpha["id"]),
            )
            for capability in ("approve", "propose", "admin"):
                connection.execute(
                    "INSERT INTO durable_memory.namespace_grant "
                    "(namespace_id, grantee_profile_id, capability, granted_by_profile_id) "
                    "VALUES (%s, %s, %s, %s)",
                    (namespace_id, self.beta["id"], capability, self.alpha["id"]),
                )
            connection.execute(
                "INSERT INTO durable_memory.record "
                "(id, namespace_id, record_type, identity_key, status, revision, "
                "search_text, payload, created_by_profile_id, updated_by_profile_id) "
                "VALUES (%s, %s, 'fact', %s, 'active', 1, 'before', %s::jsonb, %s, %s)",
                (
                    record_id,
                    namespace_id,
                    identity,
                    json.dumps({"identity": identity, "value": "before"}),
                    self.alpha["id"],
                    self.alpha["id"],
                ),
            )
            connection.execute(
                "INSERT INTO durable_memory.memory_candidate "
                "(id, namespace_id, record_type, identity_key, payload, text, "
                "submitted_by_profile_id, canonical_payload, canonical_search_text, assessment) "
                "VALUES (%s, %s, 'fact', %s, %s::jsonb, 'after', %s, %s::jsonb, "
                "'after', 'conflict')",
                (
                    candidate_id,
                    namespace_id,
                    identity,
                    json.dumps({"value": "after"}),
                    self.alpha["id"],
                    json.dumps({"value": "after"}),
                ),
            )
            connection.execute(
                "INSERT INTO durable_memory.candidate_record_relation "
                "(candidate_id, record_id, reason) VALUES (%s, %s, 'concurrency regression')",
                (candidate_id, record_id),
            )
            connection.execute(
                "INSERT INTO durable_memory.hard_purge_request "
                "(id, namespace_id, record_id, record_type, identity_key, "
                "requested_by_profile_id, reason) VALUES (%s, %s, %s, 'fact', %s, %s, %s)",
                (
                    purge_request_id,
                    namespace_id,
                    record_id,
                    identity,
                    self.alpha["id"],
                    "consolidation race regression",
                ),
            )
        purge_application = f"purge-{uuid.uuid4()}"
        consolidation_application = f"consolidation-{uuid.uuid4()}"
        purge_started = threading.Barrier(2)
        consolidation_started = threading.Barrier(2)

        def purge() -> str:
            with self._connect_as("durable_memory_beta") as connection:
                connection.execute("SET LOCAL statement_timeout = '5s'")
                connection.execute(
                    "SELECT set_config('application_name', %s, false)",
                    (purge_application,),
                )
                purge_started.wait(timeout=5)
                try:
                    connection.execute(
                        "SELECT durable_memory.approve_hard_purge(%s)",
                        (purge_request_id,),
                    ).fetchone()
                except self.psycopg.Error as error:
                    return error.sqlstate or "database-error"
            return "ok"

        def consolidate() -> str:
            with self._connect_as("durable_memory_beta") as connection:
                connection.execute("SET LOCAL statement_timeout = '5s'")
                connection.execute(
                    "SELECT set_config('application_name', %s, false)",
                    (consolidation_application,),
                )
                consolidation_started.wait(timeout=5)
                try:
                    connection.execute(
                        "SELECT durable_memory.consolidate_candidate(%s, %s, 'require', 3600)",
                        (candidate_id, str(uuid.uuid4())),
                    ).fetchone()
                except self.psycopg.Error as error:
                    return error.sqlstate or "database-error"
            return "ok"

        # When
        with self.psycopg.connect(_DATABASE_URL) as blocker:
            blocker.execute(
                "SELECT id FROM durable_memory.record WHERE id = %s FOR UPDATE",
                (record_id,),
            ).fetchone()
            with ThreadPoolExecutor(max_workers=2) as executor:
                purge_future = executor.submit(purge)
                purge_started.wait(timeout=5)
                self._wait_for_lock_wait(purge_application)
                consolidation_future = executor.submit(consolidate)
                consolidation_started.wait(timeout=5)
                self._wait_for_lock_wait(consolidation_application)
                blocker.commit()
                outcomes = {
                    purge_future.result(timeout=8),
                    consolidation_future.result(timeout=8),
                }

        # Then
        self.assertNotIn("40P01", outcomes)
        self.assertLessEqual(outcomes, {"ok", "P0001"})
        with self.psycopg.connect(_DATABASE_URL) as connection:
            terminal = connection.execute(
                "SELECT "
                "(SELECT count(*) FROM durable_memory.record WHERE id = %s), "
                "(SELECT count(*) FROM durable_memory.memory_candidate WHERE id = %s), "
                "(SELECT count(*) FROM durable_memory.hard_purge_audit WHERE request_id = %s)",
                (record_id, candidate_id, purge_request_id),
            ).fetchone()
        self.assertEqual(terminal, (0, 0, 1))

    def test_semantic_assessment_and_hard_purge_finish_without_deadlock(
        self,
    ) -> None:
        # Given
        namespace_id = str(uuid.uuid4())
        record_id = str(uuid.uuid4())
        candidate_id = str(uuid.uuid4())
        purge_request_id = str(uuid.uuid4())
        identity = f"semantic-purge:{uuid.uuid4()}"
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "INSERT INTO durable_memory.namespace "
                "(id, slug, kind, owner_profile_id) VALUES (%s, %s, 'shared', %s)",
                (namespace_id, f"semantic-purge-{uuid.uuid4()}", self.alpha["id"]),
            )
            connection.execute(
                "INSERT INTO durable_memory.namespace_grant "
                "(namespace_id, grantee_profile_id, capability, granted_by_profile_id) "
                "VALUES (%s, %s, 'admin', %s)",
                (namespace_id, self.beta["id"], self.alpha["id"]),
            )
            connection.execute(
                "INSERT INTO durable_memory.record "
                "(id, namespace_id, record_type, identity_key, status, revision, "
                "search_text, payload, created_by_profile_id, updated_by_profile_id) "
                "VALUES (%s, %s, 'fact', %s, 'active', 1, 'before', %s::jsonb, %s, %s)",
                (
                    record_id,
                    namespace_id,
                    identity,
                    json.dumps({"identity": identity, "value": "before"}),
                    self.alpha["id"],
                    self.alpha["id"],
                ),
            )
            connection.execute(
                "INSERT INTO durable_memory.memory_candidate "
                "(id, namespace_id, record_type, identity_key, payload, text, "
                "submitted_by_profile_id, canonical_payload, canonical_search_text, assessment) "
                "VALUES (%s, %s, 'fact', %s, %s::jsonb, 'after', %s, %s::jsonb, "
                "'after', 'conflict')",
                (
                    candidate_id,
                    namespace_id,
                    identity,
                    json.dumps({"value": "after"}),
                    self.alpha["id"],
                    json.dumps({"identity": identity, "value": "after"}),
                ),
            )
            connection.execute(
                "INSERT INTO durable_memory.candidate_record_relation "
                "(candidate_id, record_id, reason) VALUES (%s, %s, 'existing relation')",
                (candidate_id, record_id),
            )
            connection.execute(
                "INSERT INTO durable_memory.hard_purge_request "
                "(id, namespace_id, record_id, record_type, identity_key, "
                "requested_by_profile_id, reason) VALUES (%s, %s, %s, 'fact', %s, %s, %s)",
                (
                    purge_request_id,
                    namespace_id,
                    record_id,
                    identity,
                    self.alpha["id"],
                    "semantic assessment race regression",
                ),
            )
        purge_application = f"purge-{uuid.uuid4()}"
        assessment_application = f"assessment-{uuid.uuid4()}"
        purge_started = threading.Barrier(2)
        assessment_started = threading.Barrier(2)

        def purge() -> str:
            with self._connect_as("durable_memory_beta") as connection:
                connection.execute("SET LOCAL lock_timeout = '5s'")
                connection.execute("SET LOCAL statement_timeout = '5s'")
                connection.execute(
                    "SELECT set_config('application_name', %s, false)",
                    (purge_application,),
                )
                purge_started.wait(timeout=5)
                try:
                    connection.execute(
                        "SELECT durable_memory.approve_hard_purge(%s)",
                        (purge_request_id,),
                    ).fetchone()
                except self.psycopg.Error as error:
                    return error.sqlstate or "database-error"
            return "ok"

        def assess() -> str:
            with self._connect_as("durable_memory_alpha") as connection:
                connection.execute("SET LOCAL lock_timeout = '5s'")
                connection.execute("SET LOCAL statement_timeout = '5s'")
                connection.execute(
                    "SELECT set_config('application_name', %s, false)",
                    (assessment_application,),
                )
                assessment_started.wait(timeout=5)
                try:
                    connection.execute(
                        "SELECT durable_memory.candidate_semantic_assessment(%s)",
                        (candidate_id,),
                    ).fetchone()
                except self.psycopg.Error as error:
                    return error.sqlstate or "database-error"
            return "ok"

        # When
        with self.psycopg.connect(_DATABASE_URL) as blocker:
            blocker.execute(
                "SELECT candidate_id FROM durable_memory.candidate_record_relation "
                "WHERE candidate_id = %s FOR UPDATE",
                (candidate_id,),
            ).fetchone()
            with ThreadPoolExecutor(max_workers=2) as executor:
                purge_future = executor.submit(purge)
                purge_started.wait(timeout=5)
                self._wait_for_lock_wait(purge_application)
                assessment_future = executor.submit(assess)
                assessment_started.wait(timeout=5)
                self._wait_for_lock_wait(assessment_application)
                blocker.commit()
                outcomes = {
                    purge_future.result(timeout=8),
                    assessment_future.result(timeout=8),
                }

        # Then
        self.assertNotIn("40P01", outcomes)
        self.assertLessEqual(outcomes, {"ok", "P0001"})
        with self.psycopg.connect(_DATABASE_URL) as connection:
            terminal = connection.execute(
                "SELECT "
                "(SELECT count(*) FROM durable_memory.record WHERE id = %s), "
                "(SELECT count(*) FROM durable_memory.memory_candidate WHERE id = %s), "
                "(SELECT count(*) FROM durable_memory.candidate_record_relation "
                " WHERE candidate_id = %s), "
                "(SELECT count(*) FROM durable_memory.hard_purge_audit WHERE request_id = %s)",
                (record_id, candidate_id, candidate_id, purge_request_id),
            ).fetchone()
        self.assertEqual(terminal, (0, 0, 0, 1))

    def test_cross_semantic_assessments_lock_records_in_global_order(self) -> None:
        # Given
        namespace_id = str(uuid.uuid4())
        candidate_ids = (str(uuid.uuid4()), str(uuid.uuid4()))
        unordered_record_ids = (str(uuid.uuid4()), str(uuid.uuid4()))
        with self.psycopg.connect(_DATABASE_URL) as connection:
            ordered_record_ids = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT value::text FROM unnest(%s::uuid[]) AS value "
                    "ORDER BY hashtextextended(value::text, 0), value",
                    (list(unordered_record_ids),),
                ).fetchall()
            )
            lower_record_id, higher_record_id = ordered_record_ids
            lower_identity = f"cross-assessment-lower:{uuid.uuid4()}"
            higher_identity = f"cross-assessment-higher:{uuid.uuid4()}"
            connection.execute(
                "INSERT INTO durable_memory.namespace "
                "(id, slug, kind, owner_profile_id) VALUES (%s, %s, 'shared', %s)",
                (namespace_id, f"cross-assessment-{uuid.uuid4()}", self.alpha["id"]),
            )
            for record_id, identity in (
                (lower_record_id, lower_identity),
                (higher_record_id, higher_identity),
            ):
                connection.execute(
                    "INSERT INTO durable_memory.record "
                    "(id, namespace_id, record_type, identity_key, status, revision, "
                    "search_text, payload, created_by_profile_id, updated_by_profile_id) "
                    "VALUES (%s, %s, 'fact', %s, 'active', 1, 'canonical', "
                    "%s::jsonb, %s, %s)",
                    (
                        record_id,
                        namespace_id,
                        identity,
                        json.dumps({"identity": identity, "value": "canonical"}),
                        self.alpha["id"],
                        self.alpha["id"],
                    ),
                )
            for candidate_id, identity, relation_record_id in (
                (candidate_ids[0], higher_identity, lower_record_id),
                (candidate_ids[1], lower_identity, higher_record_id),
            ):
                connection.execute(
                    "INSERT INTO durable_memory.memory_candidate "
                    "(id, namespace_id, record_type, identity_key, payload, text, "
                    "submitted_by_profile_id, canonical_payload, "
                    "canonical_search_text, assessment) VALUES "
                    "(%s, %s, 'fact', %s, %s::jsonb, 'candidate', %s, %s::jsonb, "
                    "'candidate', 'conflict')",
                    (
                        candidate_id,
                        namespace_id,
                        identity,
                        json.dumps({"value": "candidate"}),
                        self.alpha["id"],
                        json.dumps({"identity": identity, "value": "candidate"}),
                    ),
                )
                connection.execute(
                    "INSERT INTO durable_memory.candidate_record_relation "
                    "(candidate_id, record_id, reason) VALUES (%s, %s, 'reversed target')",
                    (candidate_id, relation_record_id),
                )
        first_application = f"assessment-first-{uuid.uuid4()}"
        second_application = f"assessment-second-{uuid.uuid4()}"
        first_started = threading.Barrier(2)
        second_started = threading.Barrier(2)

        def assess(candidate_id: str, application_name: str, started) -> str:
            with self._connect_as("durable_memory_alpha") as connection:
                connection.execute("SET LOCAL lock_timeout = '5s'")
                connection.execute("SET LOCAL statement_timeout = '5s'")
                connection.execute(
                    "SELECT set_config('application_name', %s, false)",
                    (application_name,),
                )
                started.wait(timeout=5)
                try:
                    connection.execute(
                        "SELECT durable_memory.candidate_semantic_assessment(%s)",
                        (candidate_id,),
                    ).fetchone()
                except self.psycopg.Error as error:
                    return error.sqlstate or "database-error"
            return "ok"

        # When
        with (
            self.psycopg.connect(_DATABASE_URL) as lower_blocker,
            self.psycopg.connect(_DATABASE_URL) as higher_blocker,
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            lower_blocker.execute(
                "SELECT durable_memory.lock_memory_record(%s)", (lower_record_id,)
            ).fetchone()
            higher_blocker.execute(
                "SELECT durable_memory.lock_memory_record(%s)", (higher_record_id,)
            ).fetchone()
            first_future = executor.submit(
                assess, candidate_ids[0], first_application, first_started
            )
            first_started.wait(timeout=5)
            self._wait_for_lock_wait(first_application)
            second_future = executor.submit(
                assess, candidate_ids[1], second_application, second_started
            )
            second_started.wait(timeout=5)
            self._wait_for_lock_wait(second_application)
            lower_blocker.commit()
            self._wait_for_advisory_locks(first_application, (1, 1))
            higher_blocker.commit()
            outcomes = {
                first_future.result(timeout=8),
                second_future.result(timeout=8),
            }

        # Then
        self.assertNotIn("40P01", outcomes)
        self.assertEqual(outcomes, {"ok"})
        with self.psycopg.connect(_DATABASE_URL) as connection:
            relations = connection.execute(
                "SELECT candidate_id::text, record_id::text "
                "FROM durable_memory.candidate_record_relation "
                "WHERE candidate_id = ANY(%s) ORDER BY candidate_id",
                (list(candidate_ids),),
            ).fetchall()
        self.assertEqual(
            set(relations),
            {(candidate_ids[0], higher_record_id), (candidate_ids[1], lower_record_id)},
        )

    def test_hard_purge_removes_matching_pending_create_trace(self) -> None:
        # Given
        namespace_id = str(uuid.uuid4())
        namespace_slug = f"purge-pending-{uuid.uuid4()}"
        identity = f"purge-pending:{uuid.uuid4()}"
        record_id = str(uuid.uuid4())
        self.addCleanup(
            self.migrator.bootstrap_profile,
            "alpha",
            "durable_memory_alpha",
            ApprovalPolicy(create="auto"),
        )
        self.migrator.bootstrap_profile(
            "alpha", "durable_memory_alpha", ApprovalPolicy()
        )
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "INSERT INTO durable_memory.namespace "
                "(id, slug, kind, owner_profile_id) VALUES (%s, %s, 'shared', %s)",
                (namespace_id, namespace_slug, self.alpha["id"]),
            )
            connection.execute(
                "INSERT INTO durable_memory.namespace_grant "
                "(namespace_id, grantee_profile_id, capability, granted_by_profile_id) "
                "VALUES (%s, %s, 'admin', %s)",
                (namespace_id, self.beta["id"], self.alpha["id"]),
            )
        alpha_memory = DurableMemory(
            settings=Settings(
                store="postgres",
                profile="alpha",
                policy=ApprovalPolicy(),
                database_url=_role_url("durable_memory_alpha"),
            )
        )
        pending = alpha_memory.submit_candidate(
            MemoryCandidate(
                record_type="fact",
                identity_key=identity,
                namespace=namespace_slug,
                payload={"value": "pending trace"},
                text="pending purge trace",
                evidence=(
                    MemoryEvidence(
                        source_kind="test",
                        source_ref=f"pending-purge-evidence:{uuid.uuid4()}",
                        observed_at=datetime.now(timezone.utc),
                        confidence=1,
                    ),
                ),
            )
        )
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "INSERT INTO durable_memory.record "
                "(id, namespace_id, record_type, identity_key, status, revision, "
                "search_text, payload, created_by_profile_id, updated_by_profile_id) "
                "VALUES (%s, %s, 'fact', %s, 'active', 1, 'purge target', "
                "%s::jsonb, %s, %s)",
                (
                    record_id,
                    namespace_id,
                    identity,
                    json.dumps({"identity": identity, "value": "canonical"}),
                    self.alpha["id"],
                    self.alpha["id"],
                ),
            )
        alpha_store = PostgresStore(_role_url("durable_memory_alpha"))
        beta_store = PostgresStore(_role_url("durable_memory_beta"))
        purge_request = alpha_store.request_hard_purge(
            actor=alpha_store.get_or_create_profile("alpha"),
            namespace=alpha_store.get_namespace(namespace_slug),
            record_id=record_id,
            reason="pending create erasure regression",
        )

        # When
        beta_store.approve_hard_purge(
            actor=beta_store.get_or_create_profile("beta"),
            request_id=purge_request["id"],
        )

        # Then
        with self.psycopg.connect(_DATABASE_URL) as connection:
            trail = connection.execute(
                "SELECT "
                "(SELECT count(*) FROM durable_memory.record WHERE id = %s), "
                "(SELECT count(*) FROM durable_memory.change_request WHERE id = %s), "
                "(SELECT count(*) FROM durable_memory.change_request_private WHERE request_id = %s), "
                "(SELECT count(*) FROM durable_memory.memory_candidate WHERE id = %s), "
                "(SELECT count(*) FROM durable_memory.memory_evidence WHERE candidate_id = %s)",
                (
                    record_id,
                    pending["id"],
                    pending["id"],
                    pending["candidate_id"],
                    pending["candidate_id"],
                ),
            ).fetchone()
        self.assertEqual(trail, (0, 0, 0, 0, 0))

    def test_postgres_update_replace_is_explicit(self) -> None:
        # Given
        record_type = f"replace_{uuid.uuid4().hex}"
        identity = f"replace-target:{uuid.uuid4()}"
        memory = DurableMemory(
            settings=Settings(
                store="postgres",
                profile="alpha",
                policy=ApprovalPolicy(create="auto", update="require"),
                database_url=_role_url("durable_memory_alpha"),
            )
        )
        memory.execute_payload(
            f"create-inventory --type {record_type} --fields "
            '\'{"name":{"kind":"string","required":true},'
            '"note":{"kind":"string"}}\''
        )
        created = memory.execute_payload(
            f"propose --operation create --type {record_type} --identity {identity} "
            '--payload \'{"name":"Ada","note":"keep"}\''
        )

        # When
        merged = memory.execute_payload(
            f"propose --operation update --record-id {created['record_id']} "
            '--payload \'{"name":"Grace"}\''
        )
        memory.decide(merged["id"], "approve")
        after_merge = memory.store().get_record(created["record_id"]).payload
        replaced = memory.execute_payload(
            f"propose --operation update --record-id {created['record_id']} "
            '--replace true --payload \'{"name":"Lin"}\''
        )
        memory.decide(replaced["id"], "approve")
        after_replace = memory.store().get_record(created["record_id"]).payload

        # Then
        self.assertEqual(
            (
                merged["update_mode"],
                replaced["update_mode"],
                after_merge.get("note"),
                after_replace.get("note"),
            ),
            ("patch", "replace", "keep", None),
        )

    def test_approved_inventory_update_without_text_refreshes_search_text(self) -> None:
        for replace in (True, False):
            with self.subTest(replace=replace):
                # Given
                record_type = f"search_refresh_{uuid.uuid4().hex}"
                identity = f"contact:{uuid.uuid4()}"
                memory = DurableMemory(
                    settings=Settings(
                        store="postgres",
                        profile="alpha",
                        policy=ApprovalPolicy(create="auto", update="require"),
                        database_url=_role_url("durable_memory_alpha"),
                    )
                )
                memory.execute_payload(
                    f"create-inventory --type {record_type} --fields '{{}}'".format(
                        json.dumps(
                            {
                                "name": {
                                    "kind": "string",
                                    "required": True,
                                    "searchable": True,
                                },
                                "note": {"kind": "string"},
                            }
                        )
                    )
                )
                created = memory.execute_payload(
                    f"propose --operation create --type {record_type} "
                    f"--identity {identity} "
                    '--payload \'{"name":"Ada","note":"keep"}\''
                )
                replace_option = " --replace true" if replace else ""

                # When
                requested = memory.execute_payload(
                    f"propose --operation update --record-id {created['record_id']}"
                    f'{replace_option} --payload \'{{"name":"Lin"}}\''
                )
                decided = memory.decide(requested["id"], "approve")

                # Then
                record = memory.store().get_record(created["record_id"])
                lin_matches = {
                    item["identity"] for item in memory.search("Lin")["records"]
                }
                ada_matches = {
                    item["identity"] for item in memory.search("Ada")["records"]
                }
                self.assertEqual(
                    (
                        decided["status"],
                        record.payload["name"],
                        record.search_text,
                        identity in lin_matches,
                        identity in ada_matches,
                    ),
                    ("approved", "Lin", "Lin", True, False),
                )

    def test_inventory_update_preserves_explicit_search_text(self) -> None:
        # Given
        record_type = f"explicit_search_{uuid.uuid4().hex}"
        identity = f"contact:{uuid.uuid4()}"
        memory = DurableMemory(
            settings=Settings(
                store="postgres",
                profile="alpha",
                policy=ApprovalPolicy(create="auto", update="require"),
                database_url=_role_url("durable_memory_alpha"),
            )
        )
        memory.execute_payload(
            f"create-inventory --type {record_type} --fields "
            '\'{"name":{"kind":"string","required":true,'
            '"searchable":true}}\''
        )
        created = memory.execute_payload(
            f"propose --operation create --type {record_type} --identity {identity} "
            '--payload \'{"name":"Ada"}\''
        )

        # When
        requested = memory.execute_payload(
            f"propose --operation update --record-id {created['record_id']} "
            '--payload \'{"name":"Lin"}\' --text Pseudonym'
        )
        memory.decide(requested["id"], "approve")

        # Then
        record = memory.store().get_record(created["record_id"])
        matches = {item["identity"] for item in memory.search("Pseudonym")["records"]}
        self.assertEqual((record.search_text, identity in matches), ("Pseudonym", True))

    def test_update_without_inventory_definition_preserves_search_text(self) -> None:
        # Given
        identity = f"legacy:{uuid.uuid4()}"
        memory = DurableMemory(
            settings=Settings(
                store="postgres",
                profile="alpha",
                policy=ApprovalPolicy(create="auto", update="require"),
                database_url=_role_url("durable_memory_alpha"),
            )
        )
        created = memory.execute_payload(
            f"propose --operation create --type fact --identity {identity} "
            '--payload \'{"name":"Ada"}\' --text Ada'
        )

        # When
        requested = memory.execute_payload(
            f"propose --operation update --record-id {created['record_id']} "
            '--payload \'{"name":"Lin"}\''
        )
        memory.decide(requested["id"], "approve")

        # Then
        record = memory.store().get_record(created["record_id"])
        matches = {item["identity"] for item in memory.search("Ada")["records"]}
        self.assertEqual((record.search_text, identity in matches), ("Ada", True))

    def test_postgres_typed_sort_and_cursor(self) -> None:
        # Given
        record_type = f"sorted_{uuid.uuid4().hex}"
        type_id = str(uuid.uuid4())
        base_id = uuid.uuid4().hex[:-1]
        rows = (
            (str(uuid.UUID(hex=f"{base_id}1")), 100),
            (str(uuid.UUID(hex=f"{base_id}2")), 2),
            (str(uuid.UUID(hex=f"{base_id}3")), 10),
        )
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
                (
                    type_id,
                    json.dumps({"priority": {"kind": "integer", "filterable": True}}),
                    self.alpha["id"],
                ),
            )
            for record_id, priority in rows:
                connection.execute(
                    "INSERT INTO durable_memory.record "
                    "(id, namespace_id, record_type, identity_key, status, revision, "
                    "search_text, payload, created_by_profile_id, updated_by_profile_id) "
                    "VALUES (%s, %s, %s, %s, 'active', 1, 'sortable item', "
                    "%s::jsonb, %s, %s)",
                    (
                        record_id,
                        self.namespace_id,
                        record_type,
                        f"sorted:{priority}:{uuid.uuid4()}",
                        json.dumps({"priority": priority}),
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

        # When
        ordered = memory.search(
            "",
            namespace_slug="profile:alpha",
            record_type=record_type,
            sort="priority",
            limit=3,
        )
        first_page = memory.search(
            "sortable",
            namespace_slug="profile:alpha",
            record_type=record_type,
            sort="priority",
            limit=2,
        )
        second_page = memory.search(
            "sortable",
            namespace_slug="profile:alpha",
            record_type=record_type,
            sort="priority",
            limit=2,
            cursor=first_page["next_cursor"],
        )

        # Then
        ordered_values = [item["payload"]["priority"] for item in ordered["records"]]
        paged_values = [
            item["payload"]["priority"]
            for item in first_page["records"] + second_page["records"]
        ]
        self.assertEqual((ordered_values, paged_values), ([2, 10, 100], [2, 10, 100]))

    def test_sorted_cursor_must_belong_to_complete_result_set(self) -> None:
        # Given
        record_type = f"cursor_scope_{uuid.uuid4().hex}"
        other_type = f"cursor_other_{uuid.uuid4().hex}"
        other_namespace_id = str(uuid.uuid4())
        other_namespace_slug = f"cursor-shared-{uuid.uuid4()}"
        matching_id = str(uuid.uuid4())
        cursor_ids = {
            "namespace": str(uuid.uuid4()),
            "record_type": str(uuid.uuid4()),
            "query": str(uuid.uuid4()),
            "filter": str(uuid.uuid4()),
            "status": str(uuid.uuid4()),
            "validity": str(uuid.uuid4()),
        }
        with self.psycopg.connect(_DATABASE_URL) as connection:
            connection.execute(
                "INSERT INTO durable_memory.namespace "
                "(id, slug, kind, owner_profile_id) VALUES (%s, %s, 'shared', %s)",
                (other_namespace_id, other_namespace_slug, self.alpha["id"]),
            )
            rows = (
                (matching_id, self.namespace_id, record_type, "cursor scope match", 10),
                (
                    cursor_ids["namespace"],
                    other_namespace_id,
                    record_type,
                    "cursor scope match",
                    20,
                ),
                (
                    cursor_ids["record_type"],
                    self.namespace_id,
                    other_type,
                    "cursor scope match",
                    "not-numeric",
                ),
                (
                    cursor_ids["query"],
                    self.namespace_id,
                    record_type,
                    "different terms",
                    40,
                ),
                (
                    cursor_ids["filter"],
                    self.namespace_id,
                    record_type,
                    "cursor scope match",
                    1,
                ),
                (
                    cursor_ids["status"],
                    self.namespace_id,
                    record_type,
                    "cursor scope match",
                    50,
                ),
                (
                    cursor_ids["validity"],
                    self.namespace_id,
                    record_type,
                    "cursor scope match",
                    60,
                ),
            )
            for record_id, namespace_id, item_type, search_text, priority in rows:
                identity = f"item:{record_id}"
                connection.execute(
                    "INSERT INTO durable_memory.record "
                    "(id, namespace_id, record_type, identity_key, status, revision, "
                    "search_text, payload, created_by_profile_id, updated_by_profile_id) "
                    "VALUES (%s, %s, %s, %s, 'active', 1, %s, %s::jsonb, %s, %s)",
                    (
                        record_id,
                        namespace_id,
                        item_type,
                        identity,
                        search_text,
                        json.dumps({"identity": identity, "priority": priority}),
                        self.alpha["id"],
                        self.alpha["id"],
                    ),
                )
            connection.execute(
                "UPDATE durable_memory.record SET status = 'expired' WHERE id = %s",
                (cursor_ids["status"],),
            )
            connection.execute(
                "UPDATE durable_memory.record "
                "SET valid_from = now() - interval '2 minutes', "
                "valid_to = now() - interval '1 minute' "
                "WHERE id = %s",
                (cursor_ids["validity"],),
            )
        store = PostgresStore(_role_url("durable_memory_alpha"))
        profile = store.get_or_create_profile("alpha")
        namespace = store.get_namespace("profile:alpha")

        # When / Then
        for mismatch, cursor_id in cursor_ids.items():
            with self.subTest(mismatch=mismatch):
                with self.assertRaises(CommandError):
                    store.search(
                        profile=profile,
                        query="cursor scope",
                        namespace=namespace,
                        limit=2,
                        record_type=record_type,
                        filters={"priority": {"gte": 5}},
                        cursor=cursor_id,
                        sort="priority",
                        sort_kind="integer",
                    )

    def test_sorted_keyset_pagination_reaches_null_values_in_both_directions(
        self,
    ) -> None:
        # Given
        record_type = f"nullable_sort_{uuid.uuid4().hex}"
        base_id = uuid.uuid4().hex[:-1]
        rows = (
            (str(uuid.UUID(hex=f"{base_id}1")), 1),
            (str(uuid.UUID(hex=f"{base_id}2")), 2),
            (str(uuid.UUID(hex=f"{base_id}3")), None),
            (str(uuid.UUID(hex=f"{base_id}4")), None),
        )
        with self.psycopg.connect(_DATABASE_URL) as connection:
            for record_id, priority in rows:
                payload = {"identity": f"nullable:{record_id}"}
                if priority is not None:
                    payload["priority"] = priority
                connection.execute(
                    "INSERT INTO durable_memory.record "
                    "(id, namespace_id, record_type, identity_key, status, revision, "
                    "search_text, payload, created_by_profile_id, updated_by_profile_id) "
                    "VALUES (%s, %s, %s, %s, 'active', 1, 'nullable sort', "
                    "%s::jsonb, %s, %s)",
                    (
                        record_id,
                        self.namespace_id,
                        record_type,
                        payload["identity"],
                        json.dumps(payload),
                        self.alpha["id"],
                        self.alpha["id"],
                    ),
                )
        store = PostgresStore(_role_url("durable_memory_alpha"))
        profile = store.get_or_create_profile("alpha")
        expected = {
            False: [rows[0][0], rows[1][0], rows[2][0], rows[3][0]],
            True: [rows[1][0], rows[0][0], rows[3][0], rows[2][0]],
        }

        # When / Then
        for descending in (False, True):
            with self.subTest(descending=descending, cursor_value="non-null"):
                first = store.search(
                    profile=profile,
                    query="nullable",
                    limit=2,
                    record_type=record_type,
                    sort="priority",
                    sort_kind="integer",
                    descending=descending,
                )
                second = store.search(
                    profile=profile,
                    query="nullable",
                    limit=2,
                    record_type=record_type,
                    sort="priority",
                    sort_kind="integer",
                    descending=descending,
                    cursor=first[-1].id,
                )
                self.assertEqual(
                    [record.id for record in first + second], expected[descending]
                )
            with self.subTest(descending=descending, cursor_value="null"):
                first = store.search(
                    profile=profile,
                    query="nullable",
                    limit=3,
                    record_type=record_type,
                    sort="priority",
                    sort_kind="integer",
                    descending=descending,
                )
                second = store.search(
                    profile=profile,
                    query="nullable",
                    limit=3,
                    record_type=record_type,
                    sort="priority",
                    sort_kind="integer",
                    descending=descending,
                    cursor=first[-1].id,
                )
                self.assertEqual(
                    [record.id for record in first + second], expected[descending]
                )

    def test_inventory_integer_sort_accepts_values_beyond_bigint(self) -> None:
        # Given
        record_type = f"numeric_sort_{uuid.uuid4().hex}"
        values = (2, 10**40)
        with self.psycopg.connect(_DATABASE_URL) as connection:
            for value in values:
                identity = f"numeric-sort:{uuid.uuid4()}"
                connection.execute(
                    "INSERT INTO durable_memory.record "
                    "(id, namespace_id, record_type, identity_key, status, revision, "
                    "search_text, payload, created_by_profile_id, updated_by_profile_id) "
                    "VALUES (%s, %s, %s, %s, 'active', 1, 'numeric sort', "
                    "%s::jsonb, %s, %s)",
                    (
                        str(uuid.uuid4()),
                        self.namespace_id,
                        record_type,
                        identity,
                        json.dumps({"identity": identity, "priority": value}),
                        self.alpha["id"],
                        self.alpha["id"],
                    ),
                )
        store = PostgresStore(_role_url("durable_memory_alpha"))

        # When
        records = store.search(
            profile=store.get_or_create_profile("alpha"),
            query="numeric",
            limit=2,
            record_type=record_type,
            sort="priority",
            sort_kind="integer",
        )

        # Then
        self.assertEqual(
            [record.payload["priority"] for record in records], list(values)
        )

    def test_expired_record_embedding_lease_is_recovered(self) -> None:
        # Given
        retry_record_id = str(uuid.uuid4())
        exhausted_record_id = str(uuid.uuid4())
        null_retry_record_id = str(uuid.uuid4())
        null_exhausted_record_id = str(uuid.uuid4())
        with self.psycopg.connect(_DATABASE_URL) as connection:
            for record_id, identity in (
                (retry_record_id, f"lease-retry:{uuid.uuid4()}"),
                (exhausted_record_id, f"lease-exhausted:{uuid.uuid4()}"),
                (null_retry_record_id, f"null-lease-retry:{uuid.uuid4()}"),
                (null_exhausted_record_id, f"null-lease-exhausted:{uuid.uuid4()}"),
            ):
                connection.execute(
                    "INSERT INTO durable_memory.record "
                    "(id, namespace_id, record_type, identity_key, status, revision, "
                    "search_text, payload, created_by_profile_id, updated_by_profile_id) "
                    "VALUES (%s, %s, 'fact', %s, 'active', 1, 'lease recovery', "
                    "%s::jsonb, %s, %s)",
                    (
                        record_id,
                        self.namespace_id,
                        identity,
                        json.dumps({"identity": identity}),
                        self.alpha["id"],
                        self.alpha["id"],
                    ),
                )
            connection.execute(
                "UPDATE durable_memory.embedding_job SET status = 'processing', "
                "attempts = 1, max_attempts = 3, claim_token = gen_random_uuid(), "
                "claimed_at = now() - interval '20 minutes', "
                "lease_expires_at = now() - interval '5 minutes' "
                "WHERE record_id = %s",
                (retry_record_id,),
            )
            connection.execute(
                "UPDATE durable_memory.embedding_job SET status = 'processing', "
                "attempts = 3, max_attempts = 3, claim_token = gen_random_uuid(), "
                "claimed_at = now() - interval '20 minutes', "
                "lease_expires_at = now() - interval '5 minutes' "
                "WHERE record_id = %s",
                (exhausted_record_id,),
            )
            for record_id, attempts in (
                (null_retry_record_id, 1),
                (null_exhausted_record_id, 3),
            ):
                connection.execute(
                    "UPDATE durable_memory.embedding_job SET status = 'processing', "
                    "attempts = %s, max_attempts = 3, claim_token = gen_random_uuid(), "
                    "claimed_at = now(), lease_expires_at = NULL WHERE record_id = %s",
                    (attempts, record_id),
                )
        store = PostgresStore(_role_url("durable_memory_alpha"))

        # When
        affected = store.requeue_failed_embedding_jobs(
            profile=store.get_or_create_profile("alpha"), limit=10
        )

        # Then
        with self.psycopg.connect(_DATABASE_URL) as connection:
            states = connection.execute(
                "SELECT record_id::text, status, attempts, last_error, "
                "claim_token IS NULL, lease_expires_at IS NULL "
                "FROM durable_memory.embedding_job WHERE record_id = ANY(%s) "
                "ORDER BY record_id",
                (
                    [
                        retry_record_id,
                        exhausted_record_id,
                        null_retry_record_id,
                        null_exhausted_record_id,
                    ],
                ),
            ).fetchall()
        by_record = {row[0]: row[1:] for row in states}
        self.assertEqual(
            (
                affected,
                by_record[retry_record_id],
                by_record[exhausted_record_id],
            ),
            (
                2,
                ("pending", 1, None, True, True),
                ("failed", 3, "embedding lease expired", True, True),
            ),
        )
        self.assertEqual(
            by_record[null_retry_record_id],
            ("pending", 1, None, True, True),
        )
        self.assertEqual(
            by_record[null_exhausted_record_id],
            ("failed", 3, "embedding lease expired", True, True),
        )

    def test_claim_paths_recover_expired_record_and_candidate_leases(self) -> None:
        # Given
        record_id = str(uuid.uuid4())
        candidate_id = str(uuid.uuid4())
        null_record_id = str(uuid.uuid4())
        null_candidate_id = str(uuid.uuid4())
        identity = f"claim-recovery:{uuid.uuid4()}"
        with self.psycopg.connect(_DATABASE_URL) as connection:
            for target_record_id, target_identity in (
                (record_id, identity),
                (null_record_id, f"null:{identity}"),
            ):
                connection.execute(
                    "INSERT INTO durable_memory.record "
                    "(id, namespace_id, record_type, identity_key, status, revision, "
                    "search_text, payload, created_by_profile_id, updated_by_profile_id) "
                    "VALUES (%s, %s, 'fact', %s, 'active', 1, 'claim recovery', "
                    "%s::jsonb, %s, %s)",
                    (
                        target_record_id,
                        self.namespace_id,
                        target_identity,
                        json.dumps({"identity": target_identity}),
                        self.alpha["id"],
                        self.alpha["id"],
                    ),
                )
            connection.execute(
                "UPDATE durable_memory.embedding_job SET status = 'processing', "
                "attempts = 1, max_attempts = 3, claim_token = gen_random_uuid(), "
                "claimed_at = now() - interval '20 minutes', "
                "lease_expires_at = now() - interval '5 minutes' WHERE record_id = %s",
                (record_id,),
            )
            connection.execute(
                "UPDATE durable_memory.embedding_job SET status = 'processing', "
                "attempts = 1, max_attempts = 3, claim_token = gen_random_uuid(), "
                "claimed_at = now(), lease_expires_at = NULL WHERE record_id = %s",
                (null_record_id,),
            )
            for target_candidate_id, target_identity in (
                (candidate_id, f"candidate:{identity}"),
                (null_candidate_id, f"null-candidate:{identity}"),
            ):
                connection.execute(
                    "INSERT INTO durable_memory.memory_candidate "
                    "(id, namespace_id, record_type, identity_key, payload, text, "
                    "submitted_by_profile_id, canonical_payload, canonical_search_text) "
                    "VALUES (%s, %s, 'fact', %s, %s::jsonb, 'claim recovery', %s, "
                    "%s::jsonb, 'claim recovery')",
                    (
                        target_candidate_id,
                        self.namespace_id,
                        target_identity,
                        json.dumps({"identity": target_identity}),
                        self.alpha["id"],
                        json.dumps({"identity": target_identity}),
                    ),
                )
            connection.execute(
                "UPDATE durable_memory.candidate_embedding_job SET status = 'processing', "
                "attempts = 1, max_attempts = 3, claim_token = gen_random_uuid(), "
                "claimed_at = now() - interval '20 minutes', "
                "lease_expires_at = now() - interval '5 minutes' WHERE candidate_id = %s",
                (candidate_id,),
            )
            connection.execute(
                "UPDATE durable_memory.candidate_embedding_job SET status = 'processing', "
                "attempts = 1, max_attempts = 3, claim_token = gen_random_uuid(), "
                "claimed_at = now(), lease_expires_at = NULL WHERE candidate_id = %s",
                (null_candidate_id,),
            )
        store = PostgresStore(_role_url("durable_memory_alpha"))
        profile = store.get_or_create_profile("alpha")

        # When
        record_jobs = store.pending_embedding_jobs(profile=profile, limit=100)
        candidate_jobs = store.pending_candidate_embedding_jobs(
            profile=profile, limit=100
        )

        # Then
        self.assertIn(record_id, {job["record_id"] for job in record_jobs})
        self.assertIn(null_record_id, {job["record_id"] for job in record_jobs})
        self.assertIn(candidate_id, {job["candidate_id"] for job in candidate_jobs})
        self.assertIn(
            null_candidate_id, {job["candidate_id"] for job in candidate_jobs}
        )


@unittest.skipUnless(_DATABASE_URL, "DURABLE_MEMORY_TEST_DATABASE_URL is not set")
class PostgreSQLStagedUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls.fixture_lock = psycopg.connect(_DATABASE_URL)
        cls.fixture_lock.execute(
            "SELECT pg_advisory_lock(hashtext('durable_memory.integration_fixture'))"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture_lock.execute(
            "SELECT pg_advisory_unlock(hashtext('durable_memory.integration_fixture'))"
        )
        cls.fixture_lock.close()

    def test_upgrade_from_0019_preserves_and_backfills_runtime_data(self) -> None:
        # Given
        import psycopg

        migrator = DatabaseMigrator(_DATABASE_URL or "")
        migrations = migrator._migrations()
        profile_id = str(uuid.uuid4())
        namespace_id = str(uuid.uuid4())
        request_id = str(uuid.uuid4())
        record_job_ids = [str(uuid.uuid4()) for _ in range(2)]
        candidate_job_ids = [str(uuid.uuid4()) for _ in range(2)]
        try:
            with psycopg.connect(_DATABASE_URL) as connection:
                connection.execute("DROP SCHEMA IF EXISTS durable_memory CASCADE")
                for migration in migrations[:19]:
                    connection.execute(migration.sql)
                    connection.execute(
                        "INSERT INTO durable_memory.schema_migration "
                        "(version, name, checksum) VALUES (%s, %s, %s)",
                        (migration.version, migration.name, migration.checksum),
                    )
                connection.execute(
                    "INSERT INTO durable_memory.profile (id, slug, runtime_role) "
                    "VALUES (%s, %s, %s)",
                    (profile_id, "staged-profile", "durable_memory"),
                )
                connection.execute(
                    "INSERT INTO durable_memory.namespace "
                    "(id, slug, kind, owner_profile_id) VALUES (%s, %s, 'private', %s)",
                    (namespace_id, "profile:staged-profile", profile_id),
                )
                connection.execute(
                    "INSERT INTO durable_memory.import_checkpoint "
                    "(source_name, scope, checkpoint, report, updated_by_profile_id) "
                    "VALUES (%s, %s, %s, %s::jsonb, %s)",
                    (
                        "staged-source",
                        "staged-scope",
                        "page-7",
                        '{"accepted":2}',
                        profile_id,
                    ),
                )
                connection.execute(
                    "INSERT INTO durable_memory.change_request "
                    "(id, namespace_id, operation, record_type, identity_key, payload, "
                    "search_text, idempotency_key, status, policy_action, "
                    "requested_by_profile_id, expires_at) "
                    "VALUES (%s, %s, 'create', 'fact', %s, %s::jsonb, %s, %s, "
                    "'pending', 'require', %s, now() + interval '1 day')",
                    (
                        request_id,
                        namespace_id,
                        "staged:request",
                        '{"identity":"staged:request"}',
                        "staged request",
                        "staged-idempotency-key",
                        profile_id,
                    ),
                )
                connection.execute(
                    "INSERT INTO durable_memory.change_request_private "
                    "(request_id, payload, search_text) VALUES (%s, %s::jsonb, %s)",
                    (request_id, '{"identity":"staged:request"}', "staged request"),
                )
                for index, record_id in enumerate(record_job_ids, start=1):
                    identity = f"staged-record-job:{index}"
                    connection.execute(
                        "INSERT INTO durable_memory.record "
                        "(id, namespace_id, record_type, identity_key, status, revision, "
                        "search_text, payload, created_by_profile_id, updated_by_profile_id) "
                        "VALUES (%s, %s, 'fact', %s, 'active', 1, %s, %s::jsonb, %s, %s)",
                        (
                            record_id,
                            namespace_id,
                            identity,
                            identity,
                            json.dumps({"identity": identity}),
                            profile_id,
                            profile_id,
                        ),
                    )
                    connection.execute(
                        "UPDATE durable_memory.embedding_job SET status = 'processing', "
                        "attempts = %s, max_attempts = 3, claim_token = gen_random_uuid(), "
                        "claimed_at = now(), lease_expires_at = NULL WHERE record_id = %s",
                        (1 if index == 1 else 3, record_id),
                    )
                for index, candidate_id in enumerate(candidate_job_ids, start=1):
                    identity = f"staged-candidate-job:{index}"
                    connection.execute(
                        "INSERT INTO durable_memory.memory_candidate "
                        "(id, namespace_id, record_type, identity_key, payload, text, "
                        "submitted_by_profile_id, canonical_payload, canonical_search_text) "
                        "VALUES (%s, %s, 'fact', %s, %s::jsonb, %s, %s, %s::jsonb, %s)",
                        (
                            candidate_id,
                            namespace_id,
                            identity,
                            json.dumps({"identity": identity}),
                            identity,
                            profile_id,
                            json.dumps({"identity": identity}),
                            identity,
                        ),
                    )
                    connection.execute(
                        "UPDATE durable_memory.candidate_embedding_job "
                        "SET status = 'processing', attempts = %s, max_attempts = 3, "
                        "claim_token = gen_random_uuid(), claimed_at = now(), "
                        "lease_expires_at = NULL WHERE candidate_id = %s",
                        (1 if index == 1 else 3, candidate_id),
                    )

                # When
                connection.execute(migrations[19].sql)
                submitted_id = connection.execute(
                    "SELECT durable_memory.submit_change_request("
                    "%s, NULL, 'create', 'fact', %s, %s::jsonb, %s, NULL, "
                    "NULL, NULL, 'patch', %s)",
                    (
                        namespace_id,
                        "staged:new",
                        '{"identity":"staged:new"}',
                        "staged new",
                        "staged-new-key",
                    ),
                ).fetchone()[0]

                # Then
                checkpoint = connection.execute(
                    "SELECT profile_id::text, checkpoint, report "
                    "FROM durable_memory.import_checkpoint WHERE source_name = %s",
                    ("staged-source",),
                ).fetchone()
                request = connection.execute(
                    "SELECT update_mode, payload, search_text FROM durable_memory.change_request "
                    "WHERE id = %s",
                    (request_id,),
                ).fetchone()
                functions = connection.execute(
                    "SELECT "
                    "to_regprocedure(%s) IS NULL, to_regprocedure(%s) IS NOT NULL, "
                    "to_regprocedure(%s) IS NOT NULL, to_regprocedure(%s) IS NOT NULL",
                    (
                        "durable_memory.submit_change_request(uuid,uuid,text,text,text,jsonb,text,integer,timestamptz,timestamptz,text)",
                        "durable_memory.submit_change_request(uuid,uuid,text,text,text,jsonb,text,integer,timestamptz,timestamptz,text,text)",
                        "durable_memory.save_import_checkpoint(text,text,text,jsonb)",
                        "durable_memory.load_import_checkpoint(text,text)",
                    ),
                ).fetchone()
                policies = connection.execute(
                    "SELECT operation, action, ttl_seconds "
                    "FROM durable_memory.operation_policy WHERE profile_id = %s "
                    "ORDER BY operation",
                    (profile_id,),
                ).fetchall()
                record_jobs = connection.execute(
                    "SELECT job.status, job.attempts, job.last_error, "
                    "projection.lifecycle_status, projection.error_message "
                    "FROM durable_memory.embedding_job AS job "
                    "JOIN durable_memory.record_embedding AS projection "
                    "ON (projection.record_id, projection.revision) = "
                    "(job.record_id, job.revision) WHERE job.record_id = ANY(%s) "
                    "ORDER BY job.attempts",
                    (record_job_ids,),
                ).fetchall()
                candidate_jobs = connection.execute(
                    "SELECT job.status, job.attempts, job.last_error, "
                    "projection.lifecycle_status, projection.error_message "
                    "FROM durable_memory.candidate_embedding_job AS job "
                    "JOIN durable_memory.candidate_embedding AS projection "
                    "ON projection.candidate_id = job.candidate_id "
                    "WHERE job.candidate_id = ANY(%s) ORDER BY job.attempts",
                    (candidate_job_ids,),
                ).fetchall()
            self.assertEqual(checkpoint, (profile_id, "page-7", {"accepted": 2}))
            self.assertEqual(
                request,
                ("patch", {"identity": "staged:request"}, "staged request"),
            )
            self.assertEqual(functions, (True, True, True, True))
            self.assertIsNotNone(submitted_id)
            self.assertEqual(
                policies,
                [
                    ("create", "require", 86400),
                    ("delete", "require", 86400),
                    ("update", "require", 86400),
                ],
            )
            self.assertEqual(
                record_jobs,
                [
                    ("pending", 1, None, "pending", None),
                    (
                        "failed",
                        3,
                        "embedding lease expired",
                        "failed",
                        "embedding lease expired",
                    ),
                ],
            )
            self.assertEqual(
                candidate_jobs,
                [
                    ("pending", 1, None, "pending", None),
                    (
                        "failed",
                        3,
                        "candidate embedding lease expired",
                        "failed",
                        "candidate embedding lease expired",
                    ),
                ],
            )
        finally:
            with psycopg.connect(_DATABASE_URL) as connection:
                connection.execute("DROP SCHEMA IF EXISTS durable_memory CASCADE")


if __name__ == "__main__":
    unittest.main()
