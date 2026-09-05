from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from hermes_durable_memory.config import Settings
from hermes_durable_memory.models import CommandError
from hermes_durable_memory.policies import ApprovalPolicy
from hermes_durable_memory.service import DurableMemory
from hermes_durable_memory.store import PostgresStore


class UnsafeRuntimeTests(unittest.TestCase):
    def test_owner_preflight_demotes_only_authority_failures(self):
        store = PostgresStore("postgresql://example.invalid/test")
        for fault in (None, "rls", "extension", "grant"):

            def execute(sql, params=None):
                cursor = Mock()
                if "r.rolsuper" in sql:
                    cursor.fetchone.return_value = (True, True)
                elif "n.nspowner" in sql:
                    cursor.fetchone.return_value = (True,)
                elif "c.relrowsecurity" in sql:
                    cursor.fetchall.return_value = [
                        (name, fault != "rls", False, True) for name in params[0]
                    ]
                elif "FROM pg_extension" in sql:
                    cursor.fetchall.return_value = [("pgcrypto",)] + (
                        [] if fault == "extension" else [("vector",)]
                    )
                elif "FROM pg_proc" in sql:
                    cursor.fetchall.return_value = [(name,) for name in params[0]]
                elif "AS" not in sql and "save_import_checkpoint" in sql:
                    cursor.fetchone.return_value = (
                        fault != "grant",
                        True,
                        True,
                        True,
                        True,
                        True,
                        False,
                    )
                elif (
                    "has_schema_privilege" in sql
                    or "has_table_privilege" in sql
                    or "has_function_privilege" in sql
                ):
                    cursor.fetchone.return_value = (True,)
                else:
                    self.fail(f"Unexpected preflight query: {sql}")
                return cursor

            with (
                self.subTest(fault=fault),
                patch.object(store, "_connection") as connection,
            ):
                connection.return_value.__enter__.return_value.execute.side_effect = (
                    execute
                )
                self.assertFalse(store.deployment_preflight()["ok"])
                result = store.deployment_preflight(allow_unsafe_runtime=True)
                self.assertEqual(result["ok"], fault is None)
                self.assertTrue(result["warnings"])
                self.assertIn(
                    "runtime role is superuser or has BYPASSRLS", result["warnings"]
                )

    def settings(self, **extra):
        return Settings.from_env(
            {
                "DURABLE_MEMORY_STORE": "postgres",
                "DURABLE_MEMORY_PROFILE": "test",
                "DURABLE_MEMORY_DATABASE_URL": "postgresql://example.invalid/test",
                **extra,
            }
        )

    def test_flag_is_explicit_and_defaults_to_off(self):
        self.assertFalse(self.settings().allow_unsafe_runtime)
        for value in ("yes", "1", "danger", ""):
            with self.subTest(value=value), self.assertRaises(CommandError):
                self.settings(DURABLE_MEMORY_DANGER_ALLOW_UNSAFE_RUNTIME=value)
        self.assertTrue(
            self.settings(
                DURABLE_MEMORY_DANGER_ALLOW_UNSAFE_RUNTIME="true"
            ).allow_unsafe_runtime
        )

    def test_migrations_reuse_runtime_url_only_when_enabled(self):
        with self.assertRaises(CommandError):
            DurableMemory(settings=self.settings())._migrator()
        for explicit in (None, "postgresql://migration.invalid/test"):
            env = {"DURABLE_MEMORY_DANGER_ALLOW_UNSAFE_RUNTIME": "true"}
            if explicit:
                env["DURABLE_MEMORY_MIGRATION_DATABASE_URL"] = explicit
            settings = self.settings(**env)
            with patch("hermes_durable_memory.service.DatabaseMigrator") as migrator:
                DurableMemory(settings=settings)._migrator()
                migrator.assert_called_once_with(
                    explicit or settings.database_url, settings.schema
                )

    def test_danger_doctor_keeps_policy_mismatch_fatal(self):
        settings = self.settings(DURABLE_MEMORY_DANGER_ALLOW_UNSAFE_RUNTIME="true")
        store = PostgresStore(settings.database_url)
        for policy, expected in (
            (ApprovalPolicy(), True),
            (ApprovalPolicy(create="auto"), False),
        ):
            with (
                patch.object(
                    store,
                    "deployment_preflight",
                    return_value={
                        "applicable": True,
                        "ok": True,
                        "checks": [],
                        "unsafe_runtime": True,
                        "warnings": ["unsafe role"],
                    },
                ) as preflight,
                patch.object(store, "operation_policy", return_value=policy),
            ):
                result = DurableMemory(settings=settings, store=store).doctor()
                preflight.assert_called_once_with(allow_unsafe_runtime=True)
                self.assertEqual(result["postgres_ready"], expected)
                self.assertEqual(
                    result["deployment_preflight"]["warnings"], ["unsafe role"]
                )
                self.assertNotIn(settings.database_url, str(result))

    def test_danger_does_not_accept_connectivity_failure(self):
        settings = self.settings(DURABLE_MEMORY_DANGER_ALLOW_UNSAFE_RUNTIME="true")
        store = PostgresStore(settings.database_url)
        with patch.object(store, "deployment_preflight", side_effect=OSError):
            result = DurableMemory(settings=settings, store=store).doctor()
        self.assertFalse(result["postgres_ready"])
