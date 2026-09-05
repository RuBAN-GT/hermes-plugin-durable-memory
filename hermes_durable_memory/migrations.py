"""Versioned PostgreSQL schema migrations for durable memory."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from importlib.resources import files

from .i18n import t
from .models import OPERATIONS, CommandError
from .policies import ApprovalPolicy

_MIGRATION_NAME = re.compile(r"^(?P<version>\d+)_(?P<name>[a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


class DatabaseMigrator:
    """Apply ordered, checksummed SQL migrations using a migration-owner URL."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise CommandError(t("migration_url_missing"))
        self._database_url = database_url

    def status(self) -> list[dict[str, object]]:
        migrations = self._migrations()
        try:
            import psycopg
        except ImportError as error:
            raise CommandError(
                "Install psycopg to use PostgreSQL migrations."
            ) from error
        with psycopg.connect(self._database_url) as connection:
            exists = connection.execute(
                "SELECT to_regclass('durable_memory.schema_migration')"
            ).fetchone()[0]
            applied = {}
            if exists:
                rows = connection.execute(
                    "SELECT version, checksum FROM durable_memory.schema_migration"
                ).fetchall()
                applied = {row[0]: row[1] for row in rows}
        return [
            {
                "version": migration.version,
                "name": migration.name,
                "status": (
                    "pending"
                    if migration.version not in applied
                    else "applied"
                    if applied[migration.version] == migration.checksum
                    else "checksum-mismatch"
                ),
            }
            for migration in migrations
        ]

    def migrate(self) -> list[dict[str, object]]:
        migrations = self._migrations()
        try:
            import psycopg
        except ImportError as error:
            raise CommandError(
                "Install psycopg to use PostgreSQL migrations."
            ) from error
        applied: list[dict[str, object]] = []
        with psycopg.connect(self._database_url) as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext('durable_memory.schema_migration'))"
            )
            self._validate_applied(connection, migrations)
            for migration in migrations:
                existing = self._existing(connection, migration.version)
                if existing:
                    if existing != migration.checksum:
                        raise CommandError(
                            f"Migration {migration.version} has changed after "
                            "application."
                        )
                    continue
                connection.execute(migration.sql)
                connection.execute(
                    "INSERT INTO durable_memory.schema_migration "
                    "(version, name, checksum) VALUES (%s, %s, %s)",
                    (migration.version, migration.name, migration.checksum),
                )
                applied.append({"version": migration.version, "name": migration.name})
        return applied

    @staticmethod
    def _validate_applied(connection, migrations: list[Migration]) -> None:
        """Fail closed for a divergent or newer installed migration history."""
        exists = connection.execute(
            "SELECT to_regclass('durable_memory.schema_migration')"
        ).fetchone()[0]
        if not exists:
            return
        rows = connection.execute(
            "SELECT version, checksum FROM durable_memory.schema_migration ORDER BY version"
        ).fetchall()
        known = {migration.version: migration for migration in migrations}
        applied_versions = [row[0] for row in rows]
        unknown = [version for version in applied_versions if version not in known]
        if unknown:
            raise CommandError(
                "Database has migration versions unknown to this binary: "
                + ", ".join(str(version) for version in unknown)
            )
        expected_applied = list(range(1, (applied_versions[-1] if rows else 0) + 1))
        if applied_versions != expected_applied:
            raise CommandError("Applied migration versions must be contiguous.")
        for version, checksum in rows:
            if checksum != known[version].checksum:
                raise CommandError(
                    f"Migration {version} has changed after application."
                )

    def bootstrap_profile(
        self,
        slug: str,
        runtime_role: str,
        policy: ApprovalPolicy | None = None,
    ) -> dict[str, str]:
        policy = policy or ApprovalPolicy()
        if not slug or not runtime_role:
            raise CommandError("Profile slug and runtime role are required.")
        try:
            import psycopg
        except ImportError as error:
            raise CommandError("Install psycopg to bootstrap a profile.") from error
        with psycopg.connect(self._database_url) as connection:
            role = connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (runtime_role,)
            ).fetchone()
            if not role:
                raise CommandError(f"PostgreSQL role does not exist: {runtime_role}")
            row = connection.execute(
                "SELECT id::text, runtime_role FROM durable_memory.profile "
                "WHERE slug = %s",
                (slug,),
            ).fetchone()
            if row:
                if row[1] != runtime_role:
                    raise CommandError(f"Profile already uses another role: {slug}")
                profile_id = row[0]
            else:
                profile_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO durable_memory.profile (id, slug, runtime_role) "
                    "VALUES (%s, %s, %s)",
                    (profile_id, slug, runtime_role),
                )
            for operation in sorted(OPERATIONS):
                connection.execute(
                    "INSERT INTO durable_memory.operation_policy "
                    "(profile_id, operation, action, ttl_seconds) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (profile_id, operation) DO UPDATE "
                    "SET action = EXCLUDED.action, ttl_seconds = EXCLUDED.ttl_seconds",
                    (
                        profile_id,
                        operation,
                        policy.for_operation(operation),
                        policy.ttl_seconds,
                    ),
                )
        return {"id": profile_id, "slug": slug, "runtime_role": runtime_role}

    @staticmethod
    def _existing(connection, version: int) -> str | None:
        exists = connection.execute(
            "SELECT to_regclass('durable_memory.schema_migration')"
        ).fetchone()[0]
        if not exists:
            return None
        row = connection.execute(
            "SELECT checksum FROM durable_memory.schema_migration WHERE version = %s",
            (version,),
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _migrations() -> list[Migration]:
        sql_dir = files("hermes_durable_memory").joinpath("sql")
        migrations: list[Migration] = []
        for path in sql_dir.iterdir():
            match = _MIGRATION_NAME.match(path.name)
            if not match:
                continue
            sql = path.read_text(encoding="utf-8")
            migrations.append(
                Migration(
                    version=int(match.group("version")),
                    name=match.group("name"),
                    sql=sql,
                    checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                )
            )
        migrations.sort(key=lambda item: item.version)
        versions = [item.version for item in migrations]
        if len(set(versions)) != len(versions):
            raise CommandError("Migration versions must be unique.")
        if versions != list(range(1, len(versions) + 1)):
            raise CommandError("Migration versions must be contiguous starting at 1.")
        return migrations
