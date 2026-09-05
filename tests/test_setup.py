from __future__ import annotations

import argparse
import io
import os
import re
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml
from dotenv import dotenv_values
from psycopg.conninfo import conninfo_to_dict

from hermes_durable_memory.general_plugin import _cli_handler, _cli_setup
from hermes_durable_memory.models import CommandError
from hermes_durable_memory.service import DurableMemory
from hermes_durable_memory.setup_cli import run_setup
from hermes_durable_memory.setup_files import ProfileFiles, merge_env
from hermes_durable_memory.setup_plan import (
    ConnectionInput,
    SetupPlan,
    provision_database,
)


def account(user="runtime", password="synthetic-password"):
    return ConnectionInput("127.0.0.1", 5432, "test_memory", user, password)


class SetupFileTests(unittest.TestCase):
    def test_yaml_aliases_do_not_change_unrelated_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config.yaml").write_text(
                "other_plugins: &plugins\n  enabled: &enabled [existing]\n"
                "plugins: *plugins\nother_list: *enabled\n"
                "other_memory: &memory\n  provider: previous\nmemory: *memory\n"
            )
            _, rendered = ProfileFiles(home).render({}, activate=True)
            config = yaml.safe_load(rendered)
            self.assertEqual(config["other_plugins"], {"enabled": ["existing"]})
            self.assertEqual(config["other_list"], ["existing"])
            self.assertEqual(config["other_memory"], {"provider": "previous"})
            self.assertIn("durable-memory", config["plugins"]["enabled"])
            self.assertEqual(config["memory"]["provider"], "durable-memory")

    def test_invalid_yaml_uses_redacted_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config.yaml").write_text("key: [synthetic-secret\n")
            with self.assertRaises(CommandError) as raised:
                ProfileFiles(home).render({}, activate=False)
            self.assertNotIn("synthetic-secret", str(raised.exception))

    def test_env_preserves_multiline_unrelated_keys_and_removes_all_old_credentials(
        self,
    ):
        original = (
            '# keep comment\nOTHER="first\nDURABLE_MEMORY_PROFILE=not-a-binding\nlast"\n'
            "export DURABLE_MEMORY_PROFILE=old\nDURABLE_MEMORY_PROFILE=duplicate\n"
            "DURABLE_MEMORY_MIGRATION_DATABASE_URL='old\noperator-url'\nTAIL=keep"
        )
        url = account(password="synthetic:'$\\@#").url
        output = merge_env(
            original,
            {
                "DURABLE_MEMORY_PROFILE": "selected",
                "DURABLE_MEMORY_DATABASE_URL": url,
                "DURABLE_MEMORY_MIGRATION_DATABASE_URL": None,
            },
        )
        values = dotenv_values(stream=io.StringIO(output), interpolate=False)
        self.assertEqual(
            values["OTHER"], "first\nDURABLE_MEMORY_PROFILE=not-a-binding\nlast"
        )
        self.assertEqual(values["TAIL"], "keep")
        self.assertEqual(values["DURABLE_MEMORY_PROFILE"], "selected")
        self.assertEqual(values["DURABLE_MEMORY_DATABASE_URL"], url)
        self.assertNotIn("DURABLE_MEMORY_MIGRATION_DATABASE_URL", values)
        self.assertIn("# keep comment", output)
        self.assertNotIn("duplicate", output)

    def test_malformed_env_is_not_rewritten(self):
        with self.assertRaises(CommandError):
            merge_env("KEY='unclosed", {"DURABLE_MEMORY_PROFILE": "test"})

    def test_commit_updates_only_selected_home_and_preserves_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "selected"
            home.mkdir()
            other = Path(tmp) / "other"
            other.mkdir()
            (other / ".env").write_text("UNCHANGED=yes\n")
            (home / ".env").write_text("OTHER=keep\n")
            (home / "config.yaml").write_text(
                "memory:\n  provider: holographic\n  custom: keep\n"
                "plugins:\n  enabled: [existing]\n  disabled: [durable-memory, another]\n"
                "model:\n  api_key: ${MODEL_KEY}\n"
            )
            files = ProfileFiles(home)
            files.commit(
                files.render({"DURABLE_MEMORY_PROFILE": "selected"}, activate=True)
            )
            config = yaml.safe_load((home / "config.yaml").read_text())
            self.assertEqual(
                config["memory"], {"provider": "durable-memory", "custom": "keep"}
            )
            self.assertEqual(
                config["plugins"]["enabled"], ["existing", "durable-memory"]
            )
            self.assertEqual(config["plugins"]["disabled"], ["another"])
            self.assertEqual(config["model"]["api_key"], "${MODEL_KEY}")
            self.assertEqual((home / ".env").stat().st_mode & 0o777, 0o600)
            self.assertEqual((other / ".env").read_text(), "UNCHANGED=yes\n")
            again = ProfileFiles(home)
            again.commit(
                again.render({"DURABLE_MEMORY_PROFILE": "selected"}, activate=False)
            )
            self.assertEqual(yaml.safe_load((home / "config.yaml").read_text()), config)

    def test_no_activation_preserves_existing_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config.yaml").write_text("memory:\n  provider: holographic\n")
            _, config = ProfileFiles(home).render({}, activate=False)
            self.assertEqual(
                yaml.safe_load(config)["memory"]["provider"], "holographic"
            )

    def test_symlink_and_concurrent_edits_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / "target"
            target.write_text("PRIVATE=untouched\n")
            (home / ".env").symlink_to(target)
            with self.assertRaises(CommandError):
                ProfileFiles(home)
            (home / ".env").unlink()
            files = ProfileFiles(home)
            rendered = files.render({}, activate=False)
            (home / ".env").write_text("CONCURRENT=keep\n")
            with self.assertRaises(CommandError):
                files.commit(rendered)
            self.assertEqual((home / ".env").read_text(), "CONCURRENT=keep\n")
            self.assertEqual(target.read_text(), "PRIVATE=untouched\n")

    def test_config_write_failure_restores_original_env(self):
        from hermes_durable_memory.setup_files import _atomic_write

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".env").write_text("ORIGINAL=yes\n")
            files = ProfileFiles(home)

            def write(path, content):
                if path.name == "config.yaml":
                    raise OSError("synthetic failure")
                _atomic_write(path, content)

            with patch(
                "hermes_durable_memory.setup_files._atomic_write", side_effect=write
            ):
                with self.assertRaises(OSError):
                    files.commit(files.render({"NEW": "yes"}, activate=False))
            self.assertEqual((home / ".env").read_text(), "ORIGINAL=yes\n")


class SetupPlanTests(unittest.TestCase):
    def test_grant_resource_matches_documented_privileges(self):
        root = Path(__file__).parents[1]
        grants = (
            root / "hermes_durable_memory/resources/runtime_grants.sql"
        ).read_text()
        documented = next(
            block
            for block in re.findall(
                r"```sql\n(.*?)```", (root / "README.md").read_text(), re.S
            )
            if block.startswith("GRANT USAGE ON SCHEMA durable_memory")
        )
        self.assertEqual(grants, documented.replace("<runtime-role>", "{runtime_role}"))

    def test_new_roles_use_scram_verifiers_and_safe_identifiers(self):
        plan = SetupPlan(
            "test", account('role"quote'), account("owner"), admin=account("admin")
        )
        with patch("psycopg.connect") as connect:
            connection = connect.return_value.__enter__.return_value
            connection.execute.return_value.fetchone.side_effect = [None, None, None]
            connection.pgconn.encrypt_password.return_value = (
                b"SCRAM-SHA-256$synthetic-verifier"
            )
            provision_database(plan)
            statements = [str(c.args[0]) for c in connection.execute.call_args_list]
            self.assertEqual(sum("CREATE ROLE" in s for s in statements), 2)
            self.assertTrue(any("CREATE DATABASE" in s for s in statements))
            self.assertNotIn("synthetic-password", str(statements))
            self.assertTrue(any("Identifier(" in s for s in statements))

    def test_urls_roundtrip_secrets_and_socket_hosts(self):
        for host in ("localhost", "::1", "/tmp/test sockets"):
            value = ConnectionInput(
                host, 5432, "test db", "test user", "synthetic:@/\\'${TEST}"
            )
            parsed = conninfo_to_dict(value.url)
            self.assertEqual(parsed["password"], value.password)
            self.assertEqual(parsed["host"], host)
            self.assertNotIn(value.password, repr(value))

    def test_operator_secrets_are_not_persisted(self):
        plan = SetupPlan(
            "test",
            account(),
            account("owner", "owner-secret"),
            admin=account("admin", "admin-secret"),
        )
        values = plan.env_values()
        self.assertIsNone(values["DURABLE_MEMORY_MIGRATION_DATABASE_URL"])
        self.assertNotIn("owner-secret", str(values))
        self.assertNotIn("admin-secret", str(values))
        self.assertNotIn("synthetic-password", repr(plan))
        with self.assertRaises(CommandError):
            SetupPlan("test", account(), account())
        with self.assertRaises(CommandError):
            SetupPlan("../../other", account(), account(), danger=True)

    def test_setup_orders_migrations_and_checks_before_file_save(self):
        plan = SetupPlan("test", account(), account("owner"))
        with (
            patch("hermes_durable_memory.setup_plan.provision_database") as provision,
            patch("psycopg.connect") as connect,
            patch("hermes_durable_memory.service.DatabaseMigrator") as migrator,
            patch.object(
                DurableMemory, "doctor", return_value={"postgres_ready": True}
            ),
        ):
            connect.return_value.__enter__.return_value.execute.return_value.fetchone.side_effect = [
                ("runtime",),
                ("owner",),
            ]
            DurableMemory.setup_database(plan)
            provision.assert_called_once_with(plan)
            names = [call[0] for call in migrator.return_value.method_calls]
            self.assertEqual(names, ["migrate", "bootstrap_profile", "grant_runtime"])

    def test_provision_does_not_change_existing_passwords_or_owners(self):
        plan = SetupPlan("test", account(), account("owner"), admin=account("admin"))
        with patch("psycopg.connect") as connect:
            connection = connect.return_value.__enter__.return_value
            connection.execute.return_value.fetchone.side_effect = [
                ("owner",),
                (1,),
                (1,),
            ]
            provision_database(plan)
            statements = [str(c.args[0]) for c in connection.execute.call_args_list]
            self.assertFalse(
                any(
                    "CREATE ROLE" in s or "CREATE DATABASE" in s or "ALTER ROLE" in s
                    for s in statements
                )
            )
        with patch("psycopg.connect") as connect:
            connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (
                "someone_else",
            )
            with self.assertRaises(CommandError):
                provision_database(plan)
            self.assertEqual(
                connect.return_value.__enter__.return_value.execute.call_count, 1
            )


class SetupCLITests(unittest.TestCase):
    def test_target_managed_marker_blocks_setup(self):
        from unittest.mock import Mock

        from hermes_durable_memory.setup_cli import _check_managed

        config = types.ModuleType("hermes_cli.config")
        config.is_managed = Mock(return_value=False)
        scope = types.ModuleType("hermes_cli.managed_scope")
        scope.is_env_managed = Mock(return_value=False)
        scope.is_key_managed = Mock(return_value=False)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch.dict(
                sys.modules,
                {
                    "hermes_cli": types.ModuleType("hermes_cli"),
                    "hermes_cli.config": config,
                    "hermes_cli.managed_scope": scope,
                },
            ):
                _check_managed(home, {}, False)
                (home / ".managed").touch()
                with self.assertRaises(CommandError):
                    _check_managed(home, {}, False)

    def test_memory_namespace_does_not_identify_current_home(self):
        from unittest.mock import Mock

        from hermes_durable_memory.setup_cli import _profile_home

        with tempfile.TemporaryDirectory() as tmp:
            profiles = types.ModuleType("hermes_cli.profiles")
            profiles.validate_profile_name = Mock()
            profiles.resolve_profile_env = Mock(return_value=tmp)
            constants = types.ModuleType("hermes_constants")
            constants.get_hermes_home = Mock(
                side_effect=AssertionError("must not infer default")
            )
            with (
                patch.dict(
                    sys.modules,
                    {
                        "hermes_cli": types.ModuleType("hermes_cli"),
                        "hermes_cli.profiles": profiles,
                        "hermes_constants": constants,
                    },
                ),
                patch.dict(
                    os.environ,
                    {
                        "HERMES_HOME": str(Path(tmp) / "other"),
                        "DURABLE_MEMORY_PROFILE": "chosen",
                    },
                    clear=True,
                ),
            ):
                self.assertEqual(_profile_home("chosen", "chosen"), Path(tmp).resolve())
            profiles.resolve_profile_env.assert_called_once_with("chosen")

    def test_explicit_hermes_profile_keeps_custom_home(self):
        from unittest.mock import Mock

        from hermes_durable_memory.setup_cli import _profile_home

        with tempfile.TemporaryDirectory() as tmp:
            profiles = types.ModuleType("hermes_cli.profiles")
            profiles.validate_profile_name = Mock()
            profiles.resolve_profile_env = Mock(
                side_effect=AssertionError("must preserve explicit custom home")
            )
            constants = types.ModuleType("hermes_constants")
            constants.get_hermes_home = Mock(return_value=Path(tmp))
            with (
                patch.dict(
                    sys.modules,
                    {
                        "hermes_cli": types.ModuleType("hermes_cli"),
                        "hermes_cli.profiles": profiles,
                        "hermes_constants": constants,
                    },
                ),
                patch.dict(
                    os.environ,
                    {"HERMES_HOME": tmp, "HERMES_PROFILE": "chosen"},
                    clear=True,
                ),
            ):
                self.assertEqual(_profile_home("chosen", "chosen"), Path(tmp).resolve())
            profiles.resolve_profile_env.assert_not_called()

    def invoke(self, home, *, confirm="yes", failure=None):
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(
                patch.dict(
                    os.environ,
                    {"HERMES_PROFILE": "test", "DURABLE_MEMORY_PROFILE": "test"},
                )
            )
            stack.enter_context(patch("sys.stdin.isatty", return_value=True))
            stack.enter_context(
                patch(
                    "hermes_durable_memory.setup_cli._profile_home", return_value=home
                )
            )
            stack.enter_context(patch("hermes_durable_memory.setup_cli._check_managed"))
            stack.enter_context(
                patch("getpass.getpass", return_value="secret-never-print")
            )
            stack.enter_context(
                patch(
                    "builtins.input",
                    side_effect=["", "", "", "", "", "", "no", "yes", confirm],
                )
            )
            database = stack.enter_context(
                patch.object(
                    DurableMemory,
                    "setup_database",
                    return_value={"message": "ready"},
                    side_effect=failure,
                )
            )
            stack.enter_context(redirect_stdout(output))
            try:
                run_setup(danger=True)
            except SystemExit as error:
                output.write(str(error))
            return output.getvalue(), database

    def test_cancel_does_not_connect_or_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            output, database = self.invoke(home, confirm="no")
            database.assert_not_called()
            self.assertEqual(list(home.iterdir()), [])
            self.assertNotIn("secret-never-print", output)

    def test_success_saves_env_without_exposing_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            output, database = self.invoke(home)
            database.assert_called_once()
            saved = dotenv_values(home / ".env")
            self.assertEqual(saved["DURABLE_MEMORY_PROFILE"], "test")
            self.assertEqual(
                saved["DURABLE_MEMORY_DANGER_ALLOW_UNSAFE_RUNTIME"], "true"
            )
            self.assertNotIn("secret-never-print", output)

    def test_database_error_does_not_save_files_or_print_driver_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            output, database = self.invoke(
                home, failure=RuntimeError("secret-never-print")
            )
            database.assert_called_once()
            self.assertEqual(list(home.iterdir()), [])
            self.assertNotIn("secret-never-print", output)

    def test_database_error_reports_a_safe_postgresql_code(self):
        class DatabaseError(RuntimeError):
            sqlstate = "0A000"

        with tempfile.TemporaryDirectory() as tmp:
            output, _ = self.invoke(
                Path(tmp), failure=DatabaseError("secret-never-print")
            )
            self.assertIn("PostgreSQL SQLSTATE 0A000", output)
            self.assertIn("install pgvector", output)
            self.assertNotIn("secret-never-print", output)

    def test_setup_text_is_english_when_gateway_language_is_russian(self):
        with patch.dict(os.environ, {"HERMES_LANGUAGE": "ru"}):
            self.assertEqual(
                __import__("hermes_durable_memory.setup_cli", fromlist=["t"]).t(
                    "setup_profile"
                ),
                "Hermes profile",
            )

    def test_setup_dispatches_without_reading_unconfigured_settings(self):
        parser = argparse.ArgumentParser()
        _cli_setup(parser)
        args = parser.parse_args(["setup", "--target-profile", "test", "--danger"])
        with patch("hermes_durable_memory.setup_cli.run_setup") as wizard:
            _cli_handler(DurableMemory(environment={}), args)
            wizard.assert_called_once_with(profile="test", danger=True)

    def test_noninteractive_setup_refuses_password_input(self):
        with (
            patch("sys.stdin.isatty", return_value=False),
            patch("getpass.getpass") as password,
        ):
            with self.assertRaises(SystemExit):
                run_setup()
            password.assert_not_called()
