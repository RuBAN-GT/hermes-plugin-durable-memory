"""Operator setup inputs and PostgreSQL provisioning; never renders passwords."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

from .config import Settings
from .i18n import t
from .models import CommandError
from .policies import ApprovalPolicy


@dataclass(frozen=True, repr=False)
class ConnectionInput:
    host: str
    port: int
    database: str
    user: str
    password: str
    sslmode: str = "prefer"

    def __post_init__(self):
        if not self.host or not self.host.isprintable():
            raise CommandError(t("setup_input_invalid"))
        if not self.host.startswith("/") and not re.fullmatch(
            r"[A-Za-z0-9_.:\-]+", self.host
        ):
            raise CommandError(t("setup_input_invalid"))
        for name in (self.database, self.user):
            if not name or len(name.encode("utf-8")) > 63 or not name.isprintable():
                raise CommandError(t("setup_input_invalid"))
        if not 1 <= self.port <= 65535 or self.sslmode not in {
            "disable",
            "prefer",
            "require",
            "verify-ca",
            "verify-full",
        }:
            raise CommandError(t("setup_input_invalid"))
        if any(c in self.password for c in "\r\n\x00"):
            raise CommandError(t("setup_input_invalid"))

    @property
    def url(self) -> str:
        authority = (
            ""
            if self.host.startswith("/")
            else (f"[{self.host}]" if ":" in self.host else self.host)
        )
        # Unix socket paths are percent-encoded in the query, not shell syntax.
        socket = (
            "&host=" + quote(self.host, safe="") if self.host.startswith("/") else ""
        )
        return (
            f"postgresql://{quote(self.user, safe='')}:{quote(self.password, safe='')}"
            f"@{authority}:{self.port}/{quote(self.database, safe='')}"
            f"?sslmode={self.sslmode}&connect_timeout=10{socket}"
        )


@dataclass(frozen=True, repr=False)
class SetupPlan:
    profile: str
    runtime: ConnectionInput
    owner: ConnectionInput
    danger: bool = False
    admin: ConnectionInput | None = None

    def __post_init__(self):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", self.profile):
            raise CommandError(t("setup_input_invalid"))
        if (self.runtime.host, self.runtime.port, self.runtime.database) != (
            self.owner.host,
            self.owner.port,
            self.owner.database,
        ):
            raise CommandError(t("setup_input_invalid"))
        if not self.danger and self.owner.user == self.runtime.user:
            raise CommandError(t("setup_separate_roles"))
        if self.admin and (self.admin.host, self.admin.port) != (
            self.runtime.host,
            self.runtime.port,
        ):
            raise CommandError(t("setup_input_invalid"))

    def settings(self) -> Settings:
        return Settings(
            store="postgres",
            profile=self.profile,
            policy=ApprovalPolicy(),
            database_url=self.runtime.url,
            migration_database_url=self.owner.url,
            allow_unsafe_runtime=self.danger,
        )

    def env_values(self) -> dict[str, str | None]:
        return {
            "DURABLE_MEMORY_STORE": "postgres",
            "DURABLE_MEMORY_PROFILE": self.profile,
            "DURABLE_MEMORY_DATABASE_URL": self.runtime.url,
            "DURABLE_MEMORY_DANGER_ALLOW_UNSAFE_RUNTIME": str(self.danger).lower(),
            "DURABLE_MEMORY_APPROVAL_CREATE": "require",
            "DURABLE_MEMORY_APPROVAL_UPDATE": "require",
            "DURABLE_MEMORY_APPROVAL_DELETE": "require",
            "DURABLE_MEMORY_APPROVAL_TTL_SECONDS": "86400",
            # Old operator credentials must not remain in the gateway env.
            "DURABLE_MEMORY_MIGRATION_DATABASE_URL": None,
        }


def provision_database(plan: SetupPlan) -> None:
    """Create missing objects only; never rotate existing passwords or owners."""
    if plan.admin is None:
        return
    import psycopg
    from psycopg import sql

    with psycopg.connect(plan.admin.url, autocommit=True) as connection:
        existing = connection.execute(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s",
            (plan.runtime.database,),
        ).fetchone()
        if existing and existing[0] != plan.owner.user:
            raise CommandError(t("setup_database_owner"))
        for account in {
            plan.owner.user: plan.owner,
            plan.runtime.user: plan.runtime,
        }.values():
            role = connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (account.user,)
            ).fetchone()
            if not role:
                if not account.password:
                    raise CommandError(t("setup_new_role_password"))
                # Send a SCRAM verifier, never a plaintext password in DDL/logs.
                verifier = connection.pgconn.encrypt_password(
                    account.password.encode(), account.user.encode(), b"scram-sha-256"
                ).decode()
                connection.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOREPLICATION NOBYPASSRLS PASSWORD {}"
                    ).format(sql.Identifier(account.user), sql.Literal(verifier))
                )
        if not existing:
            connection.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(plan.runtime.database),
                    sql.Identifier(plan.owner.user),
                )
            )
            connection.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(plan.runtime.database)
                )
            )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(plan.runtime.database), sql.Identifier(plan.runtime.user)
            )
        )
    from dataclasses import replace

    with psycopg.connect(
        replace(plan.admin, database=plan.runtime.database).url
    ) as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
