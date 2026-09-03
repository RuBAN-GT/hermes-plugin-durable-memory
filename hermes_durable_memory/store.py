from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .i18n import t
from .models import (
    CAPABILITIES,
    NAMESPACE_KINDS,
    OPERATIONS,
    ChangeRequest,
    CommandError,
    Grant,
    Namespace,
    Profile,
    Record,
    StoreState,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def idempotency_key(
    *,
    profile_id: str,
    operation: str,
    namespace_id: str,
    identity_key: str,
    payload: dict[str, Any],
    expected_revision: int | None,
) -> str:
    material = "|".join(
        [
            profile_id,
            operation,
            namespace_id,
            identity_key,
            payload_hash(payload),
            "" if expected_revision is None else str(expected_revision),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class InMemoryStore:
    """Process-local store used for tests and local development."""

    def __init__(self) -> None:
        self._state = StoreState()

    def get_or_create_profile(self, slug: str) -> Profile:
        existing_id = self._state.profiles_by_slug.get(slug)
        if existing_id:
            return self._state.profiles[existing_id]
        profile = Profile(id=_new_id(), slug=slug)
        self._state.profiles[profile.id] = profile
        self._state.profiles_by_slug[slug] = profile.id
        return profile

    def get_profile_by_slug(self, slug: str) -> Profile:
        profile_id = self._state.profiles_by_slug.get(slug)
        if not profile_id:
            raise CommandError(t("unknown_profile", slug=slug))
        return self._state.profiles[profile_id]

    def get_or_create_private_namespace(self, profile: Profile) -> Namespace:
        slug = f"profile:{profile.slug}"
        existing_id = self._state.namespaces_by_slug.get(slug)
        if existing_id:
            return self._state.namespaces[existing_id]
        namespace = Namespace(
            id=_new_id(),
            slug=slug,
            kind="private",
            owner_profile_id=profile.id,
        )
        self._state.namespaces[namespace.id] = namespace
        self._state.namespaces_by_slug[slug] = namespace.id
        return namespace

    def create_namespace(self, *, owner: Profile, slug: str, kind: str) -> Namespace:
        if kind not in NAMESPACE_KINDS:
            raise CommandError(t("namespace_kind_invalid"))
        if slug in self._state.namespaces_by_slug:
            raise CommandError(t("namespace_exists", slug=slug))
        if kind == "private" and slug != f"profile:{owner.slug}":
            raise CommandError(t("private_namespace_owned"))
        namespace = Namespace(
            id=_new_id(),
            slug=slug,
            kind=kind,
            owner_profile_id=owner.id,
        )
        self._state.namespaces[namespace.id] = namespace
        self._state.namespaces_by_slug[slug] = namespace.id
        return namespace

    def get_namespace(self, slug: str) -> Namespace:
        namespace_id = self._state.namespaces_by_slug.get(slug)
        if not namespace_id:
            raise CommandError(t("unknown_namespace", slug=slug))
        return self._state.namespaces[namespace_id]

    def list_namespaces(self, profile: Profile) -> list[Namespace]:
        visible: list[Namespace] = []
        for namespace in self._state.namespaces.values():
            if self._has_capability(profile, namespace, "read"):
                visible.append(namespace)
        return sorted(visible, key=lambda item: item.slug)

    def grant(
        self,
        *,
        actor: Profile,
        namespace: Namespace,
        grantee: Profile,
        capability: str,
    ) -> Grant:
        if capability not in CAPABILITIES:
            raise CommandError(t("capability_invalid"))
        if not self._has_capability(actor, namespace, "admin"):
            raise CommandError(t("grant_admin_only"))
        grant = Grant(
            namespace_id=namespace.id,
            grantee_profile_id=grantee.id,
            capability=capability,
            granted_by_profile_id=actor.id,
        )
        if not any(
            item.namespace_id == grant.namespace_id
            and item.grantee_profile_id == grant.grantee_profile_id
            and item.capability == grant.capability
            for item in self._state.grants
        ):
            self._state.grants.append(grant)
        return grant

    def require_capability(
        self, profile: Profile, namespace: Namespace, capability: str
    ) -> None:
        if not self._has_capability(profile, namespace, capability):
            raise CommandError(
                t(
                    "missing_capability",
                    capability=t(f"capability_{capability}"),
                    namespace=namespace.slug,
                )
            )

    def get_record(self, record_id: str) -> Record:
        record = self._state.records.get(record_id)
        if not record:
            raise CommandError(t("unknown_record"))
        return record

    def search(
        self, *, profile: Profile, query: str, namespace: Namespace | None = None
    ) -> list[Record]:
        needle = query.casefold()
        matches: list[Record] = []
        for record in self._state.records.values():
            if record.status != "active":
                continue
            record_namespace = self._state.namespaces[record.namespace_id]
            if namespace and record.namespace_id != namespace.id:
                continue
            if not self._has_capability(profile, record_namespace, "read"):
                continue
            haystack = f"{record.search_text} {record.identity_key}".casefold()
            if needle in haystack:
                matches.append(record)
        return matches

    def propose(
        self,
        *,
        actor: Profile,
        namespace: Namespace,
        operation: str,
        record_type: str,
        identity_key: str,
        search_text: str,
        payload: dict[str, Any],
        policy_action: str,
        ttl_seconds: int,
        record_id: str | None = None,
        expected_revision: int | None = None,
    ) -> ChangeRequest:
        if operation not in OPERATIONS:
            raise CommandError(t("unknown_operation"))
        self.require_capability(actor, namespace, "propose")
        target: Record | None = None
        if operation == "create":
            if record_id:
                raise CommandError(t("create_has_record_id"))
            self._assert_identity_available(namespace.id, record_type, identity_key)
        else:
            if not record_id:
                raise CommandError(t("mutation_needs_record_id"))
            target = self.get_record(record_id)
            if target.namespace_id != namespace.id:
                raise CommandError(t("record_wrong_namespace"))
            if target.status != "active":
                raise CommandError(t("record_not_active"))
            identity_key = target.identity_key
            record_type = target.record_type
            payload = {**payload, "identity": identity_key}
            if expected_revision is None:
                expected_revision = target.revision
        key = idempotency_key(
            profile_id=actor.id,
            operation=operation,
            namespace_id=namespace.id,
            identity_key=identity_key,
            payload=payload,
            expected_revision=expected_revision,
        )
        existing_id = self._state.requests_by_key.get(key)
        if existing_id:
            return self._state.requests[existing_id]
        now = _now()
        request = ChangeRequest(
            id=_new_id(),
            namespace_id=namespace.id,
            record_id=record_id,
            operation=operation,
            record_type=record_type,
            identity_key=identity_key,
            expected_revision=expected_revision,
            payload=payload,
            search_text=search_text,
            idempotency_key=key,
            status="pending",
            policy_action=policy_action,
            requested_by_profile_id=actor.id,
            requested_at=_iso(now),
            expires_at=_iso(now + timedelta(seconds=ttl_seconds)),
        )
        self._state.requests[request.id] = request
        self._state.requests_by_key[key] = request.id
        return request

    def pending(self, profile: Profile) -> list[ChangeRequest]:
        visible: list[ChangeRequest] = []
        now = _iso(_now())
        for request in self._state.requests.values():
            namespace = self._state.namespaces[request.namespace_id]
            if not self._has_capability(profile, namespace, "approve") and (
                request.requested_by_profile_id != profile.id
            ):
                continue
            if request.status == "pending" and request.expires_at <= now:
                request.status = "expired"
            if request.status == "pending":
                visible.append(request)
        return visible

    def decide(
        self,
        *,
        actor: Profile,
        request_id: str,
        decision: str,
        require_approve_capability: bool = True,
    ) -> ChangeRequest:
        request = self._state.requests.get(request_id)
        if not request:
            raise CommandError(t("unknown_request"))
        namespace = self._state.namespaces[request.namespace_id]
        if require_approve_capability:
            self.require_capability(actor, namespace, "approve")
        if request.status != "pending":
            return request
        if request.expires_at <= _iso(_now()):
            request.status = "expired"
            request.decided_at = _iso(_now())
            return request
        if decision == "reject":
            request.status = "rejected"
            request.decided_by_profile_id = actor.id
            request.decided_at = _iso(_now())
            return request
        if decision != "approve":
            raise CommandError(t("decision_invalid"))
        return self._apply(actor=actor, request=request, namespace=namespace)

    def _apply(
        self, *, actor: Profile, request: ChangeRequest, namespace: Namespace
    ) -> ChangeRequest:
        if request.operation == "create":
            try:
                self._assert_identity_available(
                    namespace.id, request.record_type, request.identity_key
                )
            except CommandError:
                request.status = "superseded"
                request.decided_by_profile_id = actor.id
                request.decided_at = _iso(_now())
                return request
            record = Record(
                id=_new_id(),
                namespace_id=namespace.id,
                record_type=request.record_type,
                identity_key=request.identity_key,
                status="active",
                revision=1,
                search_text=request.search_text,
                payload=request.payload,
                origin="tool",
                created_by_profile_id=request.requested_by_profile_id,
                updated_by_profile_id=actor.id,
            )
            self._state.records[record.id] = record
            request.record_id = record.id
        else:
            record = self.get_record(request.record_id or "")
            if (
                request.expected_revision is not None
                and record.revision != request.expected_revision
            ):
                request.status = "superseded"
                request.decided_by_profile_id = actor.id
                request.decided_at = _iso(_now())
                return request
            if request.operation == "update":
                self._state.records[record.id] = Record(
                    id=record.id,
                    namespace_id=record.namespace_id,
                    record_type=record.record_type,
                    identity_key=record.identity_key,
                    status="active",
                    revision=record.revision + 1,
                    search_text=request.search_text or record.search_text,
                    payload=request.payload or record.payload,
                    origin=record.origin,
                    created_by_profile_id=record.created_by_profile_id,
                    updated_by_profile_id=actor.id,
                )
            else:
                self._state.records[record.id] = Record(
                    id=record.id,
                    namespace_id=record.namespace_id,
                    record_type=record.record_type,
                    identity_key=record.identity_key,
                    status="tombstoned",
                    revision=record.revision + 1,
                    search_text=record.search_text,
                    payload=record.payload,
                    origin=record.origin,
                    created_by_profile_id=record.created_by_profile_id,
                    updated_by_profile_id=actor.id,
                )
        request.status = "approved"
        request.decided_by_profile_id = actor.id
        request.decided_at = _iso(_now())
        return request

    def _assert_identity_available(
        self, namespace_id: str, record_type: str, identity_key: str
    ) -> None:
        for record in self._state.records.values():
            if (
                record.namespace_id == namespace_id
                and record.record_type == record_type
                and record.identity_key == identity_key
                and record.status == "active"
            ):
                raise CommandError(
                    t("identity_taken", type=record_type, identity=identity_key)
                )

    def _has_capability(
        self, profile: Profile, namespace: Namespace, capability: str
    ) -> bool:
        if namespace.owner_profile_id == profile.id:
            return True
        return any(
            grant.namespace_id == namespace.id
            and grant.grantee_profile_id == profile.id
            and grant.capability == capability
            for grant in self._state.grants
        )


class PostgresStore:
    """PostgreSQL-backed store; the database role identifies the profile."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise CommandError("A PostgreSQL database URL is required.")
        self._database_url = database_url

    @staticmethod
    def _psycopg():
        try:
            import psycopg
        except ImportError as error:
            raise CommandError(
                "Install psycopg to use the PostgreSQL store."
            ) from error
        return psycopg

    def _connection(self):
        return self._psycopg().connect(self._database_url)

    @staticmethod
    def _profile(row: tuple[Any, ...]) -> Profile:
        return Profile(id=str(row[0]), slug=row[1])

    @staticmethod
    def _namespace(row: tuple[Any, ...]) -> Namespace:
        return Namespace(
            id=str(row[0]), slug=row[1], kind=row[2], owner_profile_id=str(row[3])
        )

    @staticmethod
    def _record(row: tuple[Any, ...]) -> Record:
        return Record(
            id=str(row[0]),
            namespace_id=str(row[1]),
            record_type=row[2],
            identity_key=row[3],
            status=row[4],
            revision=row[5],
            search_text=row[6],
            payload=row[7],
            origin=row[8],
            created_by_profile_id=str(row[9]),
            updated_by_profile_id=str(row[10]),
        )

    @staticmethod
    def _request(row: tuple[Any, ...]) -> ChangeRequest:
        return ChangeRequest(
            id=str(row[0]),
            namespace_id=str(row[1]),
            record_id=str(row[2]) if row[2] else None,
            operation=row[3],
            record_type=row[4],
            identity_key=row[5],
            expected_revision=row[6],
            payload=row[7],
            search_text=row[8],
            idempotency_key=row[9],
            status=row[10],
            policy_action=row[11],
            requested_by_profile_id=str(row[12]),
            decided_by_profile_id=str(row[13]) if row[13] else None,
            requested_at=(row[14] if isinstance(row[14], str) else _iso(row[14])),
            decided_at=(row[15] if isinstance(row[15], str) else _iso(row[15]))
            if row[15]
            else None,
            expires_at=(row[16] if isinstance(row[16], str) else _iso(row[16])),
        )

    def get_or_create_profile(self, slug: str) -> Profile:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, slug FROM durable_memory.profile "
                "WHERE runtime_role = session_user"
            ).fetchone()
        if not row or row[1] != slug:
            raise CommandError(t("unknown_profile", slug=slug))
        return self._profile(row)

    def get_profile_by_slug(self, slug: str) -> Profile:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, slug FROM durable_memory.profile WHERE slug = %s", (slug,)
            ).fetchone()
        if not row:
            raise CommandError(t("unknown_profile", slug=slug))
        return self._profile(row)

    def get_or_create_private_namespace(self, profile: Profile) -> Namespace:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, slug, kind, owner_profile_id FROM durable_memory.namespace "
                "WHERE slug = %s",
                (f"profile:{profile.slug}",),
            ).fetchone()
            if row:
                return self._namespace(row)
            row = connection.execute(
                "INSERT INTO durable_memory.namespace "
                "(id, slug, kind, owner_profile_id) VALUES (%s, %s, 'private', %s) "
                "RETURNING id, slug, kind, owner_profile_id",
                (_new_id(), f"profile:{profile.slug}", profile.id),
            ).fetchone()
        return self._namespace(row)

    def create_namespace(self, *, owner: Profile, slug: str, kind: str) -> Namespace:
        if kind not in NAMESPACE_KINDS:
            raise CommandError(t("namespace_kind_invalid"))
        if kind == "private" and slug != f"profile:{owner.slug}":
            raise CommandError(t("private_namespace_owned"))
        with self._connection() as connection:
            try:
                row = connection.execute(
                    "INSERT INTO durable_memory.namespace "
                    "(id, slug, kind, owner_profile_id) VALUES (%s, %s, %s, %s) "
                    "RETURNING id, slug, kind, owner_profile_id",
                    (_new_id(), slug, kind, owner.id),
                ).fetchone()
            except self._psycopg().errors.UniqueViolation as error:
                raise CommandError(t("namespace_exists", slug=slug)) from error
        return self._namespace(row)

    def get_namespace(self, slug: str) -> Namespace:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, slug, kind, owner_profile_id FROM durable_memory.namespace "
                "WHERE slug = %s",
                (slug,),
            ).fetchone()
        if not row:
            raise CommandError(t("unknown_namespace", slug=slug))
        return self._namespace(row)

    def list_namespaces(self, profile: Profile) -> list[Namespace]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, slug, kind, owner_profile_id FROM durable_memory.namespace "
                "WHERE durable_memory.has_capability(id, 'read') OR "
                "durable_memory.has_capability(id, 'propose') OR "
                "durable_memory.has_capability(id, 'approve') OR "
                "durable_memory.has_capability(id, 'admin') ORDER BY slug"
            ).fetchall()
        return [self._namespace(row) for row in rows]

    def grant(
        self, *, actor: Profile, namespace: Namespace, grantee: Profile, capability: str
    ) -> Grant:
        if capability not in CAPABILITIES:
            raise CommandError(t("capability_invalid"))
        self.require_capability(actor, namespace, "admin")
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO durable_memory.namespace_grant "
                "(namespace_id, grantee_profile_id, capability, granted_by_profile_id) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (namespace.id, grantee.id, capability, actor.id),
            )
        return Grant(namespace.id, grantee.id, capability, actor.id)

    def require_capability(
        self, profile: Profile, namespace: Namespace, capability: str
    ) -> None:
        with self._connection() as connection:
            allowed = connection.execute(
                "SELECT durable_memory.has_capability(%s, %s)",
                (namespace.id, capability),
            ).fetchone()[0]
        if not allowed:
            raise CommandError(
                t(
                    "missing_capability",
                    capability=t(f"capability_{capability}"),
                    namespace=namespace.slug,
                )
            )

    def get_record(self, record_id: str) -> Record:
        with self._connection() as connection:
            row = connection.execute(
                self._record_sql("WHERE id = %s"), (record_id,)
            ).fetchone()
        if not row:
            raise CommandError(t("unknown_record"))
        return self._record(row)

    @staticmethod
    def _record_sql(condition: str = "") -> str:
        return (
            "SELECT id, namespace_id, record_type, identity_key, status, revision, "
            "search_text, payload, origin, created_by_profile_id, "
            "updated_by_profile_id FROM durable_memory.record " + condition
        )

    def search(
        self, *, profile: Profile, query: str, namespace: Namespace | None = None
    ) -> list[Record]:
        params: list[Any] = []
        condition = "WHERE status = 'active'"
        if query.strip():
            condition += (
                " AND to_tsvector('simple', search_text || ' ' || identity_key) "
                "@@ plainto_tsquery('simple', %s)"
            )
            params.append(query)
        if namespace:
            condition += " AND namespace_id = %s"
            params.append(namespace.id)
        with self._connection() as connection:
            rows = connection.execute(self._record_sql(condition), params).fetchall()
        return [self._record(row) for row in rows]

    def propose(
        self,
        *,
        actor: Profile,
        namespace: Namespace,
        operation: str,
        record_type: str,
        identity_key: str,
        search_text: str,
        payload: dict[str, Any],
        policy_action: str,
        ttl_seconds: int,
        record_id: str | None = None,
        expected_revision: int | None = None,
    ) -> ChangeRequest:
        if operation not in OPERATIONS:
            raise CommandError(t("unknown_operation"))
        self.require_capability(actor, namespace, "propose")
        with self._connection() as connection:
            if operation == "create":
                if record_id:
                    raise CommandError(t("create_has_record_id"))
                taken = connection.execute(
                    "SELECT 1 FROM durable_memory.record WHERE namespace_id = %s "
                    "AND record_type = %s AND identity_key = %s "
                    "AND status = 'active'",
                    (namespace.id, record_type, identity_key),
                ).fetchone()
                if taken:
                    raise CommandError(
                        t("identity_taken", type=record_type, identity=identity_key)
                    )
            else:
                if not record_id:
                    raise CommandError(t("mutation_needs_record_id"))
                target = connection.execute(
                    self._record_sql("WHERE id = %s"), (record_id,)
                ).fetchone()
                if not target:
                    raise CommandError(t("unknown_record"))
                current = self._record(target)
                if current.namespace_id != namespace.id:
                    raise CommandError(t("record_wrong_namespace"))
                if current.status != "active":
                    raise CommandError(t("record_not_active"))
                identity_key, record_type = current.identity_key, current.record_type
                payload = {**payload, "identity": identity_key}
                expected_revision = (
                    current.revision if expected_revision is None else expected_revision
                )
            key = idempotency_key(
                profile_id=actor.id,
                operation=operation,
                namespace_id=namespace.id,
                identity_key=identity_key,
                payload=payload,
                expected_revision=expected_revision,
            )
            row = connection.execute(
                "INSERT INTO durable_memory.change_request "
                "(id, namespace_id, record_id, operation, record_type, identity_key, "
                "expected_revision, payload, search_text, idempotency_key, status, "
                "policy_action, requested_by_profile_id, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', "
                "%s, %s, now() + (%s * interval '1 second')) "
                "ON CONFLICT (idempotency_key) DO UPDATE SET "
                "id = durable_memory.change_request.id "
                "RETURNING id, namespace_id, record_id, operation, record_type, "
                "identity_key, expected_revision, payload, search_text, "
                "idempotency_key, status, policy_action, requested_by_profile_id, "
                "decided_by_profile_id, requested_at, decided_at, expires_at",
                (
                    _new_id(),
                    namespace.id,
                    record_id,
                    operation,
                    record_type,
                    identity_key,
                    expected_revision,
                    json.dumps(payload),
                    search_text,
                    key,
                    policy_action,
                    actor.id,
                    ttl_seconds,
                ),
            ).fetchone()
        return self._request(row)

    @staticmethod
    def _request_sql(condition: str = "") -> str:
        return (
            "SELECT id, namespace_id, record_id, operation, record_type, identity_key, "
            "expected_revision, payload, search_text, idempotency_key, status, "
            "policy_action, requested_by_profile_id, decided_by_profile_id, "
            "requested_at, decided_at, expires_at FROM durable_memory.change_request "
            + condition
        )

    def pending(self, profile: Profile) -> list[ChangeRequest]:
        with self._connection() as connection:
            rows = connection.execute(
                self._request_sql(
                    "WHERE status = 'pending' AND expires_at > now() "
                    "ORDER BY requested_at"
                )
            ).fetchall()
        return [self._request(row) for row in rows]

    def decide(
        self,
        *,
        actor: Profile,
        request_id: str,
        decision: str,
        require_approve_capability: bool = True,
    ) -> ChangeRequest:
        if decision not in {"approve", "reject"}:
            raise CommandError(t("decision_invalid"))
        with self._connection() as connection:
            connection.execute(
                "SELECT durable_memory.decide_change_request(%s, %s)",
                (request_id, decision),
            )
            row = connection.execute(
                self._request_sql("WHERE id = %s"), (request_id,)
            ).fetchone()
        if not row:
            raise CommandError(t("unknown_request"))
        return self._request(row)
