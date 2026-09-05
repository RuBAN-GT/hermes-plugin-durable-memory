"""Interactive operator adapter. No secrets in arguments, summaries, or errors."""

from __future__ import annotations

import argparse
import getpass
import io
import os
import sys
import warnings
from dataclasses import replace
from pathlib import Path

from .i18n import t as _translate
from .models import CommandError
from .service import DurableMemory
from .setup_files import ProfileFiles
from .setup_plan import ConnectionInput, SetupPlan


def t(key: str, **values: object) -> str:
    """Keep standalone operator setup independent of the gateway UI language."""
    return _translate(key, language="en", **values)


def _database_failure(error: Exception) -> str:
    """Return actionable database diagnostics without exposing connection details."""
    sqlstate = str(getattr(error, "sqlstate", "") or "")
    error_type = type(error).__name__
    detail = (
        f"{error_type}; the connection failed before PostgreSQL returned an error "
        "code. Check that the server is reachable and accepts the selected role."
    )
    missing_module = getattr(error, "name", None) or "an unspecified module"
    missing_driver_hint = (
        "The active Hermes Python environment is missing a required module. "
        f"Missing module: {missing_module}. Python executable: {sys.executable}. "
        "Install the missing package in that environment, then restart the gateway."
    )
    local_hints = {
        "ImportError": missing_driver_hint,
        "ModuleNotFoundError": missing_driver_hint,
        "OperationalError": "The PostgreSQL driver could not complete its connection "
        "handshake. Check host, port, database, role, password, and TLS settings.",
    }
    if hint := local_hints.get(error_type):
        detail = f"{error_type}. {hint}"
    if sqlstate:
        detail = f"PostgreSQL SQLSTATE {sqlstate}"
    hints = {
        "08001": "Check that PostgreSQL is reachable at the selected host and port.",
        "08004": "Check the server authentication and connection policy.",
        "08006": "The PostgreSQL connection was lost; check the server logs.",
        "0A000": "If this occurred while enabling extensions, install pgvector on the PostgreSQL server.",
        "28000": "Check that the selected database role is allowed to connect.",
        "28P01": "Check the password or external authentication for the selected role.",
        "3D000": "The selected database does not exist; enable provisioning or create it first.",
        "42501": "The selected role lacks a required PostgreSQL privilege.",
        "57P03": "PostgreSQL is starting or does not yet accept connections.",
    }
    hint = hints.get(sqlstate)
    if hint:
        detail = f"{detail}. {hint}"
    return (
        f"Database setup failed while preparing or checking the database ({detail}). "
        "Profile files were not saved. Database changes may have completed."
    )


def _ask(key: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{t(key)}{suffix}: ").strip() or default
    if not value or any(c in value for c in "\r\n\x00"):
        raise CommandError(t("setup_input_invalid"))
    return value


def _yes(key: str, *, default: bool = False) -> bool:
    while True:
        value = input(f"{t(key)} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes", "д", "да"}:
            return True
        if value in {"n", "no", "н", "нет"}:
            return False
        print(t("setup_yes_no"))


def _password(key: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(t(key) + ": ")


def add_setup_arguments(parser: argparse.ArgumentParser) -> None:
    """Add safe prefill options; credentials stay off the command line."""
    parser.add_argument("--target-profile", dest="profile", help=t("setup_profile"))
    parser.add_argument("--danger", action="store_true", help=t("setup_single_role"))
    parser.add_argument("--host", help=t("setup_host"))
    parser.add_argument("--port", type=int, help=t("setup_port"))
    parser.add_argument("--database", help=t("setup_database"))
    parser.add_argument("--schema", help=t("setup_schema"))
    parser.add_argument(
        "--sslmode",
        choices=("disable", "prefer", "require", "verify-ca", "verify-full"),
        help=t("setup_sslmode"),
    )
    parser.add_argument("--runtime-user", dest="user", help=t("setup_runtime_user"))
    parser.add_argument(
        "--provision",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=t("setup_provision"),
    )
    parser.add_argument(
        "--activate",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=t("setup_activate"),
    )


def _profile_home(profile: str, current: str) -> Path:
    from hermes_cli.profiles import resolve_profile_env, validate_profile_name
    from hermes_constants import get_hermes_home

    validate_profile_name(profile)
    # An explicitly selected current profile may have a custom HERMES_HOME.
    # Otherwise use Hermes' resolver for the explicitly entered name only.
    home = (
        get_hermes_home()
        if profile == current
        and profile == (os.environ.get("HERMES_PROFILE") or "").strip()
        and os.environ.get("HERMES_HOME")
        else Path(resolve_profile_env(profile))
    )
    home = Path(home).expanduser().resolve(strict=True)
    if not home.is_dir():
        raise CommandError(t("setup_profile_missing"))
    return home


def _check_managed(home: Path, keys: dict[str, str | None], activate: bool) -> None:
    from hermes_cli.config import is_managed
    from hermes_cli.managed_scope import is_env_managed, is_key_managed

    config_keys = ["plugins.enabled", "plugins.disabled"]
    if activate:
        config_keys.append("memory.provider")
    if (
        is_managed()
        or (home / ".managed").exists()
        or (home / ".managed").is_symlink()
        or any(is_env_managed(k) for k in keys)
        or any(is_key_managed(k) for k in config_keys)
    ):
        raise CommandError(t("setup_managed"))


def run_setup(
    *,
    profile: str | None = None,
    danger: bool = False,
    host: str | None = None,
    port: int | None = None,
    database: str | None = None,
    schema: str | None = None,
    sslmode: str | None = None,
    user: str | None = None,
    provision: bool | None = None,
    activate: bool | None = None,
) -> None:
    if not sys.stdin.isatty():
        raise SystemExit(t("setup_tty_required"))
    stage = "setup_stage_questions"
    try:
        current = (
            os.environ.get("DURABLE_MEMORY_PROFILE")
            or os.environ.get("HERMES_PROFILE")
            or ""
        ).strip()
        other = (os.environ.get("HERMES_PROFILE") or "").strip()
        if current and other and current != other:
            raise CommandError(t("setup_profile_conflict"))
        selected = profile or _ask("setup_profile", current)
        home = _profile_home(selected, current)
        files = ProfileFiles(home)
        from dotenv.parser import parse_stream

        for binding in parse_stream(
            io.StringIO((files.original_env or b"").decode("utf-8-sig"))
        ):
            if (
                binding.key == "HERMES_PROFILE"
                and binding.value
                and binding.value != selected
            ):
                raise CommandError(t("setup_profile_conflict"))
        danger = danger or _yes("setup_single_role")
        if danger:
            print(t("unsafe_runtime_warning"))
        host = host or _ask("setup_host", "127.0.0.1")
        port = port if port is not None else int(_ask("setup_port", "5432"))
        database = database or _ask("setup_database", "durable_memory")
        schema = schema or _ask("setup_schema", "durable_memory")
        sslmode = sslmode or _ask("setup_sslmode", "prefer")
        user = user or _ask("setup_runtime_user", "durable_memory")
        runtime = ConnectionInput(
            host, port, database, user, _password("setup_runtime_password"), sslmode
        )
        owner = runtime
        if not danger:
            owner = replace(
                runtime,
                user=_ask("setup_owner_user", "durable_memory_owner"),
                password=_password("setup_owner_password"),
            )
        admin = None
        should_provision = (
            provision if provision is not None else _yes("setup_provision")
        )
        if should_provision:
            admin = replace(
                runtime,
                database=_ask("setup_admin_database", "postgres"),
                user=_ask("setup_admin_user", "postgres"),
                password=_password("setup_admin_password"),
            )
        activate = activate if activate is not None else _yes("setup_activate")
        plan = SetupPlan(selected, runtime, owner, danger, admin, schema)
        values = plan.env_values()
        _check_managed(home, values, activate)
        rendered = files.render(values, activate=activate)
        print(
            t(
                "setup_summary",
                profile=selected,
                home=home,
                host=host,
                port=port,
                database=database,
                schema=schema,
                user=user,
                owner=owner.user,
                sslmode=sslmode,
                danger=str(danger).lower(),
                provision=str(admin is not None).lower(),
                activate=str(activate).lower(),
            )
        )
        if not _yes("setup_confirm"):
            print(t("setup_cancelled"))
            return
        stage = "setup_stage_database"
        print(t(stage))
        DurableMemory.setup_database(plan)
        stage = "setup_stage_files"
        _check_managed(home, values, activate)
        files.commit(rendered)
        print(t("setup_database_ready"))
        print(t("setup_complete", home=home, profile=selected))
    except (KeyboardInterrupt, EOFError):
        raise SystemExit(t("setup_interrupted", stage=t(stage))) from None
    except CommandError as error:
        if stage == "setup_stage_database":
            raise SystemExit(
                "Database setup failed while preparing or checking the database: "
                f"{str(error)} Profile files were not saved."
            ) from None
        raise SystemExit(str(error)) from None
    except Exception as error:
        if stage == "setup_stage_database":
            raise SystemExit(_database_failure(error)) from None
        raise SystemExit(t("setup_failed", stage=t(stage))) from None


def main() -> None:
    parser = argparse.ArgumentParser(description=t("setup_help"))
    add_setup_arguments(parser)
    args = parser.parse_args()
    run_setup(**vars(args))


if __name__ == "__main__":
    main()
