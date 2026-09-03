from __future__ import annotations

import os
from dataclasses import dataclass

from .i18n import t
from .models import CommandError
from .policies import ApprovalPolicy

_POLICY_KEYS = {
    "create": "DURABLE_MEMORY_APPROVAL_CREATE",
    "update": "DURABLE_MEMORY_APPROVAL_UPDATE",
    "delete": "DURABLE_MEMORY_APPROVAL_DELETE",
}


@dataclass(frozen=True)
class Settings:
    store: str
    profile: str
    policy: ApprovalPolicy
    database_url: str | None = None
    migration_database_url: str | None = None
    embedding_provider: str | None = None
    ollama_base_url: str | None = None
    ollama_model: str | None = None
    ollama_timeout_seconds: float = 10.0

    def __repr__(self) -> str:
        return (
            f"Settings(store={self.store!r}, profile={self.profile!r}, "
            f"policy={self.policy!r})"
        )

    @classmethod
    def from_env(cls, environment: dict[str, str] | None = None) -> Settings:
        env = environment if environment is not None else os.environ
        store = (env.get("DURABLE_MEMORY_STORE") or "memory").strip().lower()
        if store not in {"memory", "postgres"}:
            raise CommandError(t("store_invalid"))
        profile = (
            env.get("DURABLE_MEMORY_PROFILE") or env.get("HERMES_PROFILE") or "default"
        ).strip()
        if not profile:
            raise CommandError(t("profile_empty"))
        ttl_raw = env.get("DURABLE_MEMORY_APPROVAL_TTL_SECONDS", "86400")
        try:
            ttl_seconds = int(ttl_raw)
        except ValueError as error:
            raise CommandError(t("ttl_invalid")) from error
        database_url = (env.get("DURABLE_MEMORY_DATABASE_URL") or "").strip() or None
        migration_database_url = (
            env.get("DURABLE_MEMORY_MIGRATION_DATABASE_URL") or ""
        ).strip() or None
        embedding_provider = (
            env.get("DURABLE_MEMORY_EMBEDDING_PROVIDER") or ""
        ).strip().lower() or None
        ollama_base_url = (
            env.get("DURABLE_MEMORY_OLLAMA_BASE_URL") or ""
        ).strip() or None
        ollama_model = (env.get("DURABLE_MEMORY_OLLAMA_MODEL") or "").strip() or None
        timeout_raw = env.get("DURABLE_MEMORY_OLLAMA_TIMEOUT_SECONDS", "10")
        try:
            ollama_timeout_seconds = float(timeout_raw)
        except ValueError:
            ollama_timeout_seconds = 10.0
        if ollama_timeout_seconds <= 0:
            ollama_timeout_seconds = 10.0
        return cls(
            store=store,
            profile=profile,
            policy=ApprovalPolicy(
                create=(env.get(_POLICY_KEYS["create"]) or "require").strip(),
                update=(env.get(_POLICY_KEYS["update"]) or "require").strip(),
                delete=(env.get(_POLICY_KEYS["delete"]) or "require").strip(),
                ttl_seconds=ttl_seconds,
            ),
            database_url=database_url,
            migration_database_url=migration_database_url,
            embedding_provider=embedding_provider,
            ollama_base_url=ollama_base_url,
            ollama_model=ollama_model,
            ollama_timeout_seconds=ollama_timeout_seconds,
        )
