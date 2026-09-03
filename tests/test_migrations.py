from __future__ import annotations

import json
import os
import unittest
import uuid

from hermes_durable_memory.config import Settings
from hermes_durable_memory.migrations import DatabaseMigrator
from hermes_durable_memory.policies import ApprovalPolicy
from hermes_durable_memory.service import DurableMemory

_DATABASE_URL = os.environ.get("DURABLE_MEMORY_TEST_DATABASE_URL")


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
                "GRANT SELECT, INSERT, UPDATE ON durable_memory.record "
                "TO durable_memory_alpha, durable_memory_beta"
            )
            connection.execute(
                "GRANT SELECT, INSERT ON durable_memory.record_revision "
                "TO durable_memory_alpha, durable_memory_beta"
            )
            connection.execute(
                "GRANT SELECT, INSERT, UPDATE ON durable_memory.change_request "
                "TO durable_memory_alpha, durable_memory_beta"
            )
            connection.execute(
                "GRANT EXECUTE ON FUNCTION "
                "durable_memory.decide_change_request(uuid, text) "
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
        return self.psycopg.connect(
            _DATABASE_URL,
            user=role,
            password=f"{role}-password",
        )

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
            ],
        )

    def test_runtime_store_persists_inventory_records(self) -> None:
        memory = DurableMemory(
            settings=Settings(
                store="postgres",
                profile="alpha",
                policy=ApprovalPolicy(create="auto"),
                database_url=(
                    "postgresql://durable_memory_alpha:durable_memory_alpha-password"
                    "@127.0.0.1:55432/durable_memory_test"
                ),
            )
        )
        definition = memory.execute_payload(
            "create-inventory --type movie --fields "
            '\'{"title":{"kind":"string","required":true,'
            '"searchable":true},"rating":{"kind":"integer",'
            '"filterable":true}}\''
        )
        self.assertEqual(definition["status"], "approved")
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

    def test_runtime_role_cannot_spoof_another_profile_with_a_custom_guc(self) -> None:
        with self._connect_as("durable_memory_alpha") as connection:
            alpha_count = connection.execute(
                "SELECT count(*) FROM durable_memory.record"
            ).fetchone()[0]
            self.assertEqual(alpha_count, 1)
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
                    "UPDATE durable_memory.record SET search_text = 'bypass' "
                    "WHERE id = %s",
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

    def test_change_requests_follow_requester_and_approver_visibility(self) -> None:
        with self._connect_as("durable_memory_alpha") as connection:
            request_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO durable_memory.change_request "
                "(id, namespace_id, operation, record_type, identity_key, payload, "
                "search_text, idempotency_key, status, policy_action, "
                "requested_by_profile_id, expires_at) "
                "VALUES (%s, %s, 'create', 'fact', 'user:city', %s::jsonb, "
                "'Lives in Lisbon', %s, 'pending', 'require', %s, "
                "now() + interval '1 day')",
                (
                    request_id,
                    self.namespace_id,
                    json.dumps({"identity": "user:city", "text": "Lives in Lisbon"}),
                    str(uuid.uuid4()),
                    self.alpha["id"],
                ),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM durable_memory.change_request"
                ).fetchone()[0],
                1,
            )
        with self._connect_as("durable_memory_beta") as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM durable_memory.change_request"
                ).fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
