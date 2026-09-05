from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from math import isfinite
from typing import Any

from .database import DEFAULT_SCHEMA, SchemaConnection, validate_schema
from .i18n import t
from .models import (
    CAPABILITIES,
    INVENTORY_DEFINITION_TYPE,
    NAMESPACE_KINDS,
    OPERATIONS,
    CandidateAssessment,
    CandidateRelation,
    ChangeRequest,
    CommandError,
    Grant,
    InventoryDefinition,
    InventoryField,
    MemoryCandidate,
    Namespace,
    Profile,
    Record,
    StoreState,
)
from .policies import ApprovalPolicy


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _is_current(record: Record, now: datetime | None = None) -> bool:
    now = now or _now()
    return (
        record.status == "active"
        and (record.valid_from is None or record.valid_from <= now)
        and (record.valid_to is None or record.valid_to > now)
    )


def payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_embedding(vector: list[float]) -> list[float]:
    if not isinstance(vector, list) or not vector or len(vector) > 2000:
        raise CommandError("Embedding must contain between 1 and 2000 values.")
    if not all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(value)
        for value in vector
    ):
        raise CommandError("Embedding values must be finite numbers.")
    normalized = [float(value) for value in vector]
    if not any(value != 0 for value in normalized):
        raise CommandError("Embedding must not have zero norm.")
    return normalized


def idempotency_key(
    *,
    profile_id: str,
    operation: str,
    namespace_id: str,
    record_id: str | None,
    record_type: str,
    identity_key: str,
    payload: dict[str, Any],
    search_text: str,
    expected_revision: int | None,
    update_mode: str,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
) -> str:
    material = json.dumps(
        {
            "expected_revision": expected_revision,
            "identity_key": identity_key,
            "namespace_id": namespace_id,
            "operation": operation,
            "payload": payload,
            "profile_id": profile_id,
            "record_id": record_id,
            "record_type": record_type,
            "search_text": search_text,
            "update_mode": update_mode,
            "valid_from": valid_from.isoformat() if valid_from else None,
            "valid_to": valid_to.isoformat() if valid_to else None,
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def merge_patch(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Apply RFC 7396-style JSON merge patch without mutating either input."""
    result = dict(current)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_patch(result[key], value)
        else:
            result[key] = value
    return result


def _payload_matches_filters(payload: dict[str, Any], filters: dict[str, Any]) -> bool:
    """Evaluate already-validated filters before applying an in-memory limit."""
    for name, expected in filters.items():
        actual = payload.get(name)
        operations = (
            expected.items() if isinstance(expected, dict) else (("eq", expected),)
        )
        for operator, bound in operations:
            operator = operator.removeprefix("$")
            if operator == "eq" and actual != bound:
                return False
            if operator == "ne" and actual == bound:
                return False
            if operator == "gt" and not actual > bound:
                return False
            if operator == "gte" and not actual >= bound:
                return False
            if operator == "lt" and not actual < bound:
                return False
            if operator == "lte" and not actual <= bound:
                return False
            if operator == "in" and actual not in bound:
                return False
            if operator == "contains" and bound not in actual:
                return False
    return True


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
            existing = self._state.namespaces[existing_id]
            if existing.kind != "private" or existing.owner_profile_id != profile.id:
                raise CommandError(t("private_namespace_taken"))
            return existing
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
        if kind == "shared" and slug.startswith("profile:"):
            raise CommandError(t("private_namespace_reserved"))
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
            if any(
                self._has_capability(profile, namespace, capability)
                for capability in CAPABILITIES
            ):
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

    def get_record_for_proposal(
        self, profile: Profile, namespace: Namespace, record_id: str | None
    ) -> Record:
        if not record_id:
            raise CommandError(t("mutation_needs_record_id"))
        self.require_capability(profile, namespace, "propose")
        record = self.get_record(record_id)
        if record.namespace_id != namespace.id:
            raise CommandError(t("record_wrong_namespace"))
        if record.status != "active":
            raise CommandError(t("record_not_active"))
        return record

    def get_inventory_definition_for_proposal(
        self, profile: Profile, namespace: Namespace, record_type: str
    ) -> InventoryDefinition | None:
        self.require_capability(profile, namespace, "propose")
        return self._state.inventory_definitions.get((namespace.id, record_type))

    def get_inventory_definition(
        self, profile: Profile, namespace: Namespace, record_type: str
    ) -> InventoryDefinition | None:
        self.require_capability(profile, namespace, "read")
        return self._state.inventory_definitions.get((namespace.id, record_type))

    def list_inventory_definitions(
        self, profile: Profile, namespace: Namespace
    ) -> list[InventoryDefinition]:
        self.require_capability(profile, namespace, "read")
        return sorted(
            (
                definition
                for (namespace_id, _), definition in (
                    self._state.inventory_definitions.items()
                )
                if namespace_id == namespace.id
                and definition.lifecycle_status == "active"
            ),
            key=lambda definition: definition.record_type,
        )

    def propose_inventory(
        self,
        *,
        actor: Profile,
        namespace: Namespace,
        record_type: str,
        fields: dict[str, dict[str, Any]],
        policy_action: str,
        ttl_seconds: int,
    ) -> ChangeRequest:
        self.require_capability(actor, namespace, "propose")
        if (namespace.id, record_type) in self._state.inventory_definitions:
            raise CommandError(t("inventory_exists", type=record_type))
        return self.propose(
            actor=actor,
            namespace=namespace,
            operation="create",
            record_type=INVENTORY_DEFINITION_TYPE,
            identity_key=record_type,
            search_text="",
            payload={"identity": record_type, "fields": fields},
            policy_action=policy_action,
            ttl_seconds=ttl_seconds,
            inventory_definition=True,
        )

    def search(
        self,
        *,
        profile: Profile,
        query: str,
        namespace: Namespace | None = None,
        limit: int = 8,
        record_type: str | None = None,
        filters: dict[str, Any] | None = None,
        include_inventory: bool = False,
        cursor: str | None = None,
        sort: str | None = None,
        sort_kind: str | None = None,
        descending: bool = False,
    ) -> list[Record]:
        needle = query.casefold()
        matches: list[Record] = []
        for record in self._state.records.values():
            if not _is_current(record):
                continue
            if record.record_type == INVENTORY_DEFINITION_TYPE:
                continue
            record_namespace = self._state.namespaces[record.namespace_id]
            if namespace and record.namespace_id != namespace.id:
                continue
            if record_type and record.record_type != record_type:
                continue
            if filters and not _payload_matches_filters(record.payload, filters):
                continue
            if not self._has_capability(profile, record_namespace, "read"):
                continue
            haystack = f"{record.search_text} {record.identity_key}".casefold()
            if needle in haystack:
                matches.append(record)
        if sort:
            present = [item for item in matches if item.payload.get(sort) is not None]
            missing = [item for item in matches if item.payload.get(sort) is None]

            def sort_value(item: Record):
                value = item.payload[sort]
                match sort_kind:
                    case "integer" | "number" | "decimal":
                        return Decimal(str(value)), item.id
                    case "date":
                        return date.fromisoformat(str(value)), item.id
                    case "datetime":
                        return datetime.fromisoformat(
                            str(value).replace("Z", "+00:00")
                        ), item.id
                    case _:
                        return str(value), item.id

            present.sort(key=sort_value, reverse=descending)
            missing.sort(key=lambda item: item.id, reverse=descending)
            matches = present + missing
        else:
            matches.sort(key=lambda item: item.id, reverse=descending)
        if cursor:
            ids = [item.id for item in matches]
            try:
                matches = matches[ids.index(cursor) + 1 :]
            except ValueError:
                raise CommandError(
                    "Search cursor is invalid for this result set."
                ) from None
        return matches[:limit]

    def vector_search(self, **_kwargs: Any) -> list[tuple[Record, float]]:
        """The test store deliberately has no synthetic semantic retrieval."""
        return []

    def pending_candidate_embedding_jobs(self, **_kwargs: Any) -> list[dict[str, Any]]:
        """The test store deliberately has no synthetic semantic assessment."""
        return []

    def fail_candidate_embedding_job(self, **_kwargs: Any) -> None:
        return None

    def complete_candidate_embedding_job(self, **_kwargs: Any) -> None:
        return None

    def assess_candidate_semantics(self, **_kwargs: Any) -> None:
        return None

    def requeue_failed_embedding_jobs(self, **_kwargs: Any) -> int:
        return 0

    def requeue_failed_candidate_embedding_jobs(self, **_kwargs: Any) -> int:
        return 0

    def expire_records(self, *, profile: Profile, limit: int) -> int:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 500
        ):
            raise CommandError("Expiration limit must be between 1 and 500.")
        now = _now()
        affected = 0
        for record in sorted(self._state.records.values(), key=lambda item: item.id):
            if affected >= limit:
                break
            if (
                record.status != "active"
                or record.valid_to is None
                or record.valid_to > now
            ):
                continue
            namespace = self._state.namespaces[record.namespace_id]
            self.require_capability(profile, namespace, "approve")
            self._state.records[record.id] = Record(
                **{
                    **record.__dict__,
                    "status": "expired",
                    "updated_by_profile_id": profile.id,
                }
            )
            affected += 1
        return affected

    def set_retention_policy(
        self, *, actor: Profile, namespace: Namespace, retention_seconds: int | None
    ) -> None:
        self.require_capability(actor, namespace, "admin")
        if retention_seconds is not None and (
            not isinstance(retention_seconds, int)
            or isinstance(retention_seconds, bool)
            or not 1 <= retention_seconds <= 315576000
        ):
            raise CommandError("Retention must be between 1 second and 10 years.")
        self._state.namespace_retention_seconds[namespace.id] = retention_seconds

    def export_namespace(
        self, *, profile: Profile, namespace: Namespace
    ) -> dict[str, Any]:
        self.require_capability(profile, namespace, "read")
        records = [
            record.as_dict()
            for record in self._state.records.values()
            if record.namespace_id == namespace.id
        ]
        return {
            "namespace": {"slug": namespace.slug, "kind": namespace.kind},
            "retention_seconds": self._state.namespace_retention_seconds.get(
                namespace.id
            ),
            "records": sorted(records, key=lambda item: item["id"]),
        }

    def request_hard_purge(
        self, *, actor: Profile, namespace: Namespace, record_id: str, reason: str
    ) -> dict[str, Any]:
        self.require_capability(actor, namespace, "admin")
        if not reason.strip():
            raise CommandError("A hard purge reason is required.")
        record = self.get_record(record_id)
        if record.namespace_id != namespace.id:
            raise CommandError(t("record_wrong_namespace"))
        request = {
            "id": _new_id(),
            "namespace_id": namespace.id,
            "record_id": record.id,
            "requested_by_profile_id": actor.id,
            "reason": reason.strip(),
            "status": "pending",
            "requested_at": _iso(_now()),
        }
        self._state.hard_purge_requests[request["id"]] = request
        return dict(request)

    def approve_hard_purge(self, *, actor: Profile, request_id: str) -> dict[str, Any]:
        request = self._state.hard_purge_requests.get(request_id)
        if not request:
            raise CommandError("Unknown hard purge request.")
        namespace = self._state.namespaces[request["namespace_id"]]
        self.require_capability(actor, namespace, "admin")
        if request["requested_by_profile_id"] == actor.id:
            raise CommandError(
                "A different namespace administrator must approve a hard purge."
            )
        if request["status"] != "pending":
            return dict(request)
        record = self.get_record(request["record_id"])
        self._state.hard_purge_audit.append(
            {
                "request_id": request_id,
                "namespace_id": namespace.id,
                "record_id": record.id,
                "record_type": record.record_type,
                "identity_key": record.identity_key,
                "revision": record.revision,
                "requested_by_profile_id": request["requested_by_profile_id"],
                "approved_by_profile_id": actor.id,
                "reason": request["reason"],
                "purged_at": _iso(_now()),
            }
        )
        del self._state.records[record.id]
        request.update(
            status="purged", approved_by_profile_id=actor.id, approved_at=_iso(_now())
        )
        return dict(request)

    def load_import_checkpoint(
        self, *, source: str, scope: str
    ) -> dict[str, Any] | None:
        checkpoint = self._state.importer_checkpoints.get((source, scope))
        return dict(checkpoint) if checkpoint else None

    def save_import_checkpoint(
        self, *, source: str, scope: str, checkpoint: str | None, report: dict[str, Any]
    ) -> None:
        self._state.importer_checkpoints[(source, scope)] = {
            "checkpoint": checkpoint,
            "report": dict(report),
        }

    def deployment_preflight(self) -> dict[str, Any]:
        return {
            "applicable": False,
            "ok": True,
            "checks": [
                "PostgreSQL preflight is not applicable to the in-memory store."
            ],
        }

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
        update_mode: str = "patch",
        record_id: str | None = None,
        expected_revision: int | None = None,
        inventory_definition: bool = False,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> ChangeRequest:
        if operation not in OPERATIONS:
            raise CommandError(t("unknown_operation"))
        if record_type == INVENTORY_DEFINITION_TYPE and not inventory_definition:
            raise CommandError(t("inventory_definition_immutable"))
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
            if target.record_type == INVENTORY_DEFINITION_TYPE:
                raise CommandError(t("inventory_definition_immutable"))
            identity_key = target.identity_key
            record_type = target.record_type
            payload = {**payload, "identity": identity_key}
            if expected_revision is None:
                expected_revision = target.revision
        key = idempotency_key(
            profile_id=actor.id,
            operation=operation,
            namespace_id=namespace.id,
            record_id=record_id,
            record_type=record_type,
            identity_key=identity_key,
            payload=payload,
            search_text=search_text,
            expected_revision=expected_revision,
            update_mode=update_mode,
            valid_from=valid_from,
            valid_to=valid_to,
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
            update_mode=update_mode,
            payload=payload,
            search_text=search_text,
            idempotency_key=key,
            status="pending",
            policy_action=policy_action,
            requested_by_profile_id=actor.id,
            requested_at=_iso(now),
            expires_at=_iso(now + timedelta(seconds=ttl_seconds)),
            valid_from=valid_from,
            valid_to=valid_to,
        )
        self._state.requests[request.id] = request
        self._state.requests_by_key[key] = request.id
        return request

    def submit_candidate(
        self,
        *,
        actor: Profile,
        namespace: Namespace,
        candidate: MemoryCandidate,
        payload: dict[str, Any],
        search_text: str,
        policy_action: str,
        ttl_seconds: int,
    ) -> tuple[str, ChangeRequest | None, CandidateAssessment]:
        self.require_capability(actor, namespace, "propose")
        candidate_id = _new_id()
        self._state.candidates[candidate_id] = candidate
        self._state.candidate_namespaces[candidate_id] = namespace.id
        self._state.candidate_submitters[candidate_id] = actor.id
        self._state.candidate_payloads[candidate_id] = payload
        self._state.candidate_search_texts[candidate_id] = search_text
        matched = next(
            (
                record
                for record in sorted(
                    self._state.records.values(), key=lambda item: item.id
                )
                if record.namespace_id == namespace.id
                and record.record_type == candidate.record_type
                and record.identity_key == candidate.identity_key
                and _is_current(record)
            ),
            None,
        )
        if matched:
            status = (
                "duplicate"
                if matched.payload == payload and matched.search_text == search_text
                else "conflict"
            )
            assessment = CandidateAssessment(
                status=status,
                relation=CandidateRelation(
                    record_id=matched.id,
                    reason="exact_identity_and_equal_content"
                    if status == "duplicate"
                    else "exact_identity_with_different_content",
                ),
            )
            self._state.candidate_assessments[candidate_id] = assessment
            return candidate_id, None, assessment
        assessment = CandidateAssessment(status="new")
        self._state.candidate_assessments[candidate_id] = assessment
        request = self.propose(
            actor=actor,
            namespace=namespace,
            operation="create",
            record_type=candidate.record_type,
            identity_key=candidate.identity_key,
            search_text=search_text,
            payload=payload,
            policy_action=policy_action,
            ttl_seconds=ttl_seconds,
            update_mode="patch",
            valid_from=candidate.valid_from,
            valid_to=candidate.valid_to,
        )
        self._state.candidate_request_ids[candidate_id] = request.id
        return candidate_id, request, assessment

    def get_candidate(self, profile: Profile, candidate_id: str) -> MemoryCandidate:
        candidate = self._state.candidates.get(candidate_id)
        if not candidate:
            raise CommandError("Unknown memory candidate.")
        namespace = self._state.namespaces[
            self._state.candidate_namespaces[candidate_id]
        ]
        if self._state.candidate_submitters[candidate_id] != profile.id and not (
            self._has_capability(profile, namespace, "approve")
            or self._has_capability(profile, namespace, "admin")
        ):
            raise CommandError("Memory candidate is not visible to this profile.")
        return candidate

    def candidate_for_consolidation(
        self, profile: Profile, candidate_id: str
    ) -> tuple[MemoryCandidate, dict[str, Any], str, Record]:
        candidate = self._state.candidates.get(candidate_id)
        assessment = self._state.candidate_assessments.get(candidate_id)
        if not candidate or not assessment or not assessment.relation:
            raise CommandError(
                "Memory candidate has no matching record to consolidate."
            )
        namespace = self._state.namespaces[
            self._state.candidate_namespaces[candidate_id]
        ]
        self.require_capability(profile, namespace, "approve")
        record = self.get_record(assessment.relation.record_id)
        if record.status != "active":
            raise CommandError(t("record_not_active"))
        return (
            candidate,
            self._state.candidate_payloads[candidate_id],
            self._state.candidate_search_texts[candidate_id],
            record,
        )

    def consolidate_candidate(
        self,
        *,
        actor: Profile,
        candidate_id: str,
        policy_action: str,
        ttl_seconds: int,
    ) -> ChangeRequest:
        existing_id = self._state.candidate_consolidation_request_ids.get(candidate_id)
        if existing_id:
            return self._state.requests[existing_id]
        candidate, candidate_payload, candidate_text, record = (
            self.candidate_for_consolidation(actor, candidate_id)
        )
        namespace_id = self._state.candidate_namespaces[candidate_id]
        namespace = self._state.namespaces[namespace_id]
        if record.namespace_id != namespace.id:
            raise CommandError(t("record_wrong_namespace"))
        if record.record_type != candidate.record_type or (
            record.identity_key != candidate.identity_key
        ):
            raise CommandError("Candidate relation does not match its record.")
        payload = merge_patch(record.payload, candidate_payload)
        payload["identity"] = record.identity_key
        request_key = idempotency_key(
            profile_id=actor.id,
            operation="update",
            namespace_id=namespace.id,
            record_id=record.id,
            record_type=record.record_type,
            identity_key=record.identity_key,
            payload=payload,
            search_text=candidate_text,
            expected_revision=record.revision,
            update_mode="patch",
        )
        key = hashlib.sha256(f"{request_key}|{candidate_id}".encode()).hexdigest()
        now = _now()
        request = ChangeRequest(
            id=_new_id(),
            namespace_id=namespace.id,
            record_id=record.id,
            operation="update",
            record_type=record.record_type,
            identity_key=record.identity_key,
            expected_revision=record.revision,
            update_mode="patch",
            payload=payload,
            search_text=candidate_text,
            idempotency_key=key,
            status="pending",
            policy_action=policy_action,
            requested_by_profile_id=actor.id,
            requested_at=_iso(now),
            expires_at=_iso(now + timedelta(seconds=ttl_seconds)),
        )
        self._state.requests[request.id] = request
        self._state.requests_by_key[key] = request.id
        self._state.candidate_consolidation_request_ids[candidate_id] = request.id
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
            if request.record_type == INVENTORY_DEFINITION_TYPE:
                key = (namespace.id, request.identity_key)
                if key in self._state.inventory_definitions:
                    request.status = "superseded"
                    request.decided_by_profile_id = actor.id
                    request.decided_at = _iso(_now())
                    return request
                fields = {
                    name: InventoryField(**spec)
                    for name, spec in request.payload["fields"].items()
                }
                self._state.inventory_definitions[key] = InventoryDefinition(
                    request.identity_key, namespace.id, fields
                )
                request.status = "approved"
                request.decided_by_profile_id = actor.id
                request.decided_at = _iso(_now())
                return request
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
                valid_from=request.valid_from or _now(),
                valid_to=request.valid_to
                or (
                    (request.valid_from or _now())
                    + timedelta(
                        seconds=self._state.namespace_retention_seconds[namespace.id]
                    )
                    if self._state.namespace_retention_seconds.get(namespace.id)
                    else None
                ),
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
                    valid_from=request.valid_from or record.valid_from,
                    valid_to=request.valid_to,
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
                    valid_from=record.valid_from,
                    valid_to=record.valid_to,
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
                and _is_current(record)
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

    def __init__(self, database_url: str, schema: str = DEFAULT_SCHEMA) -> None:
        if not database_url:
            raise CommandError("A PostgreSQL database URL is required.")
        self._database_url = database_url
        self._schema = validate_schema(schema)

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
        return SchemaConnection(
            self._psycopg().connect(self._database_url), self._schema
        )

    def _table_reference(self, table: str) -> str:
        """Build an internal table reference for privilege catalog functions."""
        return f"{self._schema}.{table}"

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
            valid_from=row[11],
            valid_to=row[12],
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
            update_mode=row[7],
            payload=row[8],
            search_text=row[9],
            idempotency_key=row[10],
            status=row[11],
            policy_action=row[12],
            requested_by_profile_id=str(row[13]),
            decided_by_profile_id=str(row[14]) if row[14] else None,
            requested_at=(row[15] if isinstance(row[15], str) else _iso(row[15])),
            decided_at=(row[16] if isinstance(row[16], str) else _iso(row[16]))
            if row[16]
            else None,
            expires_at=(row[17] if isinstance(row[17], str) else _iso(row[17])),
            valid_from=row[18],
            valid_to=row[19],
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
        slug = f"profile:{profile.slug}"
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, slug, kind, owner_profile_id "
                "FROM durable_memory.namespace WHERE slug = %s",
                (slug,),
            ).fetchone()
            if row:
                namespace = self._namespace(row)
                if (
                    namespace.kind != "private"
                    or namespace.owner_profile_id != profile.id
                ):
                    raise CommandError(t("private_namespace_taken"))
                return namespace
            row = connection.execute(
                "INSERT INTO durable_memory.namespace "
                "(id, slug, kind, owner_profile_id) VALUES (%s, %s, 'private', %s) "
                "ON CONFLICT DO NOTHING "
                "RETURNING id, slug, kind, owner_profile_id",
                (_new_id(), slug, profile.id),
            ).fetchone()
            if not row:
                row = connection.execute(
                    "SELECT id, slug, kind, owner_profile_id "
                    "FROM durable_memory.namespace WHERE slug = %s",
                    (slug,),
                ).fetchone()
        if not row:
            raise CommandError(t("private_namespace_taken"))
        namespace = self._namespace(row)
        if namespace.kind != "private" or namespace.owner_profile_id != profile.id:
            raise CommandError(t("private_namespace_taken"))
        return namespace

    def create_namespace(self, *, owner: Profile, slug: str, kind: str) -> Namespace:
        if kind not in NAMESPACE_KINDS:
            raise CommandError(t("namespace_kind_invalid"))
        if kind == "shared" and slug.startswith("profile:"):
            raise CommandError(t("private_namespace_reserved"))
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

    def get_record_for_proposal(
        self, profile: Profile, namespace: Namespace, record_id: str | None
    ) -> Record:
        if not record_id:
            raise CommandError(t("mutation_needs_record_id"))
        self.require_capability(profile, namespace, "propose")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT durable_memory.proposal_record(%s)", (record_id,)
            ).fetchone()
        if not row:
            raise CommandError(t("unknown_record"))
        value = row[0]
        if isinstance(value, str):
            value = json.loads(value)
        record = Record(
            id=str(value["id"]),
            namespace_id=str(value["namespace_id"]),
            record_type=value["record_type"],
            identity_key=value["identity_key"],
            status=value["status"],
            revision=value["revision"],
            # proposal_record deliberately returns metadata only. Callers use it
            # for target validation, never to construct a merged update payload.
            search_text="",
            payload={},
            origin="",
            created_by_profile_id="",
            updated_by_profile_id="",
        )
        if record.namespace_id != namespace.id:
            raise CommandError(t("record_wrong_namespace"))
        if record.status != "active":
            raise CommandError(t("record_not_active"))
        return record

    def get_inventory_definition_for_proposal(
        self, profile: Profile, namespace: Namespace, record_type: str
    ) -> InventoryDefinition | None:
        self.require_capability(profile, namespace, "propose")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT durable_memory.proposal_inventory_definition(%s, %s)",
                (namespace.id, record_type),
            ).fetchone()
        if not row or row[0] is None:
            return None
        value = row[0]
        if isinstance(value, str):
            value = json.loads(value)
        return InventoryDefinition(
            record_type=value["record_type"],
            namespace_id=str(value["namespace_id"]),
            fields={
                name: InventoryField(**spec) for name, spec in value["fields"].items()
            },
            version=value["version"],
            lifecycle_status=value["lifecycle_status"],
        )

    def get_inventory_definition(
        self, profile: Profile, namespace: Namespace, record_type: str
    ) -> InventoryDefinition | None:
        self.require_capability(profile, namespace, "read")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT record_type, namespace_id::text, fields, version, "
                "lifecycle_status, semantic_assessment_required, archetype, sensitivity, mutable "
                "FROM durable_memory.inventory_definition "
                "WHERE namespace_id = %s AND record_type = %s "
                "AND lifecycle_status = 'active'",
                (namespace.id, record_type),
            ).fetchone()
        if not row:
            return None
        return InventoryDefinition(
            record_type=row[0],
            namespace_id=row[1],
            fields={name: InventoryField(**spec) for name, spec in row[2].items()},
            version=row[3],
            lifecycle_status=row[4],
            semantic_assessment_required=row[5],
            archetype=row[6],
            sensitivity=row[7],
            mutable=row[8],
        )

    def list_inventory_definitions(
        self, profile: Profile, namespace: Namespace
    ) -> list[InventoryDefinition]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT record_type, namespace_id::text, fields, version, "
                "lifecycle_status, semantic_assessment_required, archetype, sensitivity, mutable "
                "FROM durable_memory.inventory_definition WHERE namespace_id = %s "
                "AND lifecycle_status = 'active' ORDER BY record_type",
                (namespace.id,),
            ).fetchall()
        return [
            InventoryDefinition(
                record_type=row[0],
                namespace_id=row[1],
                fields={name: InventoryField(**spec) for name, spec in row[2].items()},
                version=row[3],
                lifecycle_status=row[4],
                semantic_assessment_required=row[5],
                archetype=row[6],
                sensitivity=row[7],
                mutable=row[8],
            )
            for row in rows
        ]

    def propose_inventory(
        self,
        *,
        actor: Profile,
        namespace: Namespace,
        record_type: str,
        fields: dict[str, dict[str, Any]],
        policy_action: str,
        ttl_seconds: int,
    ) -> ChangeRequest:
        if self.get_inventory_definition_for_proposal(actor, namespace, record_type):
            raise CommandError(t("inventory_exists", type=record_type))
        return self.propose(
            actor=actor,
            namespace=namespace,
            operation="create",
            record_type=INVENTORY_DEFINITION_TYPE,
            identity_key=record_type,
            search_text="",
            payload={"identity": record_type, "fields": fields},
            policy_action=policy_action,
            ttl_seconds=ttl_seconds,
            inventory_definition=True,
        )

    @staticmethod
    def _record_sql(condition: str = "") -> str:
        return (
            "SELECT id, namespace_id, record_type, identity_key, status, revision, "
            "search_text, payload, origin, created_by_profile_id, "
            "updated_by_profile_id, valid_from, valid_to FROM durable_memory.record "
            + condition
        )

    def search(
        self,
        *,
        profile: Profile,
        query: str,
        namespace: Namespace | None = None,
        limit: int = 8,
        record_type: str | None = None,
        filters: dict[str, Any] | None = None,
        include_inventory: bool = False,
        cursor: str | None = None,
        sort: str | None = None,
        sort_kind: str | None = None,
        descending: bool = False,
    ) -> list[Record]:
        params: list[Any] = []
        condition = (
            "WHERE status = 'active' AND valid_from <= now() "
            "AND (valid_to IS NULL OR valid_to > now())"
        )
        condition += " AND record_type <> '__inventory_definition__'"
        if query.strip():
            condition += (
                " AND to_tsvector('simple', search_text || ' ' || identity_key) "
                "@@ plainto_tsquery('simple', %s)"
            )
            params.append(query)
        if namespace:
            condition += " AND namespace_id = %s"
            params.append(namespace.id)
        if record_type:
            condition += " AND record_type = %s"
            params.append(record_type)
        if filters:
            filter_sql, filter_params = self._filter_sql(filters)
            condition += filter_sql
            params.extend(filter_params)
        sort_expression = {
            "integer": "(payload ->> %s)::numeric",
            "number": "(payload ->> %s)::numeric",
            "decimal": "(payload ->> %s)::numeric",
            "date": "(payload ->> %s)::date",
            "datetime": "(payload ->> %s)::timestamptz",
        }.get(sort_kind, "payload ->> %s")
        with self._connection() as connection:
            if cursor and sort:
                cursor_row = connection.execute(
                    "WITH cursor_record AS MATERIALIZED ("
                    "SELECT payload FROM durable_memory.record "
                    f"{condition} AND id = %s::uuid) "
                    f"SELECT {sort_expression} FROM cursor_record",
                    (*params, cursor, sort),
                ).fetchone()
                if not cursor_row:
                    raise CommandError("Search cursor is invalid for this result set.")
                comparator = "<" if descending else ">"
                cursor_value = cursor_row[0]
                if cursor_value is None:
                    condition += (
                        f" AND {sort_expression} IS NULL AND id {comparator} %s::uuid"
                    )
                    params.extend((sort, cursor))
                else:
                    condition += (
                        f" AND ({sort_expression} IS NULL OR "
                        f"{sort_expression} {comparator} %s OR "
                        f"({sort_expression} = %s AND id {comparator} %s::uuid))"
                    )
                    params.extend(
                        (sort, sort, cursor_value, sort, cursor_value, cursor)
                    )
            elif cursor:
                comparator = "<" if descending else ">"
                condition += f" AND id {comparator} %s::uuid"
                params.append(cursor)
            order = " ORDER BY id"
            if sort:
                direction = "DESC" if descending else "ASC"
                order = f" ORDER BY {sort_expression} {direction} NULLS LAST, id {direction}"
                params.append(sort)
            elif descending:
                order = " ORDER BY id DESC"
            if query.strip() and not sort:
                order = (
                    " ORDER BY ts_rank_cd(to_tsvector('simple', search_text || ' ' || "
                    "identity_key), plainto_tsquery('simple', %s)) DESC, id"
                )
                params.append(query)
            params.append(limit)
            rows = connection.execute(
                self._record_sql(condition) + order + " LIMIT %s", params
            ).fetchall()
        return [self._record(row) for row in rows]

    @staticmethod
    def _filter_sql(filters: dict[str, Any]) -> tuple[str, list[Any]]:
        """Build parameterized JSON filters for a single, validated record type."""
        clauses: list[str] = []
        params: list[Any] = []
        for name, expected in filters.items():
            operations = (
                expected.items() if isinstance(expected, dict) else (("eq", expected),)
            )
            for operator, bound in operations:
                operator = operator.removeprefix("$")
                encoded = json.dumps(bound, ensure_ascii=False)
                if operator == "eq":
                    clauses.append(" AND payload -> %s = %s::jsonb")
                    params.extend((name, encoded))
                elif operator == "ne":
                    clauses.append(" AND COALESCE(payload -> %s <> %s::jsonb, true)")
                    params.extend((name, encoded))
                elif operator in {"gt", "gte", "lt", "lte"}:
                    symbol = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[operator]
                    clauses.append(f" AND payload -> %s {symbol} %s::jsonb")
                    params.extend((name, encoded))
                elif operator == "in":
                    clauses.append(
                        " AND payload -> %s = ANY(ARRAY(SELECT jsonb_array_elements(%s::jsonb)))"
                    )
                    params.extend((name, encoded))
                elif operator == "contains":
                    clauses.append(
                        " AND (CASE jsonb_typeof(payload -> %s) "
                        "WHEN 'string' THEN strpos(payload ->> %s, %s) > 0 "
                        "WHEN 'array' THEN payload -> %s @> jsonb_build_array(%s::jsonb) "
                        "WHEN 'object' THEN payload -> %s ? %s ELSE false END)"
                    )
                    params.extend(
                        (name, name, str(bound), name, encoded, name, str(bound))
                    )
                else:  # Service validation prevents this branch.
                    raise CommandError(f"Unknown filter operator: {operator}.")
        return "".join(clauses), params

    def vector_search(
        self,
        *,
        profile: Profile,
        query_vector: list[float],
        model_identifier: str,
        namespace: Namespace | None = None,
        limit: int = 8,
        record_type: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[tuple[Record, float]]:
        vector = (
            "["
            + ",".join(str(value) for value in validate_embedding(query_vector))
            + "]"
        )
        params: list[Any] = [vector, model_identifier]
        condition = (
            "WHERE record.status = 'active' AND record.valid_from <= now() "
            "AND (record.valid_to IS NULL OR record.valid_to > now()) "
            "AND record.record_type <> '__inventory_definition__' "
            "AND projection.lifecycle_status = 'indexed' "
            "AND projection.revision = record.revision "
            "AND projection.model_identifier = %s"
        )
        if namespace:
            condition += " AND record.namespace_id = %s"
            params.append(namespace.id)
        if record_type:
            condition += " AND record.record_type = %s"
            params.append(record_type)
        if filters:
            filter_sql, filter_params = self._filter_sql(filters)
            condition += filter_sql.replace("payload", "record.payload")
            params.extend(filter_params)
        params.extend([vector, limit])
        sql = (
            "SELECT record.id, record.namespace_id, record.record_type, "
            "record.identity_key, record.status, record.revision, record.search_text, "
            "record.payload, record.origin, record.created_by_profile_id, "
            "record.updated_by_profile_id, record.valid_from, record.valid_to, "
            "projection.embedding <=> %s::vector "
            "FROM durable_memory.record AS record "
            "JOIN durable_memory.record_embedding AS projection "
            "ON projection.record_id = record.id "
            + condition
            + " ORDER BY projection.embedding <=> %s::vector, record.id LIMIT %s"
        )
        try:
            with self._connection() as connection:
                rows = connection.execute(sql, params).fetchall()
        except self._psycopg().Error:
            return []
        return [(self._record(row[:-1]), float(row[-1])) for row in rows]

    def expire_records(self, *, profile: Profile, limit: int) -> int:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 500
        ):
            raise CommandError("Expiration limit must be between 1 and 500.")
        with self._connection() as connection:
            return int(
                connection.execute(
                    "SELECT durable_memory.expire_records(%s)", (limit,)
                ).fetchone()[0]
            )

    def set_retention_policy(
        self, *, actor: Profile, namespace: Namespace, retention_seconds: int | None
    ) -> None:
        if retention_seconds is not None and (
            not isinstance(retention_seconds, int)
            or isinstance(retention_seconds, bool)
            or not 1 <= retention_seconds <= 315576000
        ):
            raise CommandError("Retention must be between 1 second and 10 years.")
        with self._connection() as connection:
            connection.execute(
                "SELECT durable_memory.set_namespace_retention(%s, %s)",
                (namespace.id, retention_seconds),
            )

    def export_namespace(
        self, *, profile: Profile, namespace: Namespace
    ) -> dict[str, Any]:
        self.require_capability(profile, namespace, "read")
        with self._connection() as connection:
            records = connection.execute(
                self._record_sql("WHERE namespace_id = %s ORDER BY id"), (namespace.id,)
            ).fetchall()
            revisions = connection.execute(
                "SELECT revision, operation, payload, created_at FROM durable_memory.record_revision "
                "WHERE record_id IN (SELECT id FROM durable_memory.record WHERE namespace_id = %s) "
                "ORDER BY record_id, revision",
                (namespace.id,),
            ).fetchall()
            retention = connection.execute(
                "SELECT durable_memory.namespace_retention(%s)", (namespace.id,)
            ).fetchone()
        return {
            "namespace": {"slug": namespace.slug, "kind": namespace.kind},
            "retention_seconds": retention[0] if retention else None,
            "records": [record.as_dict() for record in map(self._record, records)],
            "revisions": [
                {
                    "revision": row[0],
                    "operation": row[1],
                    "payload": row[2],
                    "created_at": _iso(row[3]),
                }
                for row in revisions
            ],
        }

    def request_hard_purge(
        self, *, actor: Profile, namespace: Namespace, record_id: str, reason: str
    ) -> dict[str, Any]:
        if not reason.strip():
            raise CommandError("A hard purge reason is required.")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id::text, status, requested_at FROM durable_memory.request_hard_purge(%s, %s, %s)",
                (namespace.id, record_id, reason.strip()),
            ).fetchone()
        return {"id": row[0], "status": row[1], "requested_at": _iso(row[2])}

    def approve_hard_purge(self, *, actor: Profile, request_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id::text, status, approved_at FROM durable_memory.approve_hard_purge(%s)",
                (request_id,),
            ).fetchone()
        return {
            "id": row[0],
            "status": row[1],
            "approved_at": _iso(row[2]) if row[2] else None,
        }

    def load_import_checkpoint(
        self, *, source: str, scope: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT checkpoint, report FROM durable_memory.load_import_checkpoint(%s, %s)",
                (source, scope),
            ).fetchone()
        return {"checkpoint": row[0], "report": row[1]} if row else None

    def save_import_checkpoint(
        self, *, source: str, scope: str, checkpoint: str | None, report: dict[str, Any]
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                "SELECT durable_memory.save_import_checkpoint(%s, %s, %s, %s::jsonb)",
                (source, scope, checkpoint, json.dumps(report)),
            )

    def deployment_preflight(
        self, *, allow_unsafe_runtime: bool = False
    ) -> dict[str, Any]:
        sensitive_tables = (
            "namespace",
            "namespace_grant",
            "record",
            "record_revision",
            "change_request",
            "memory_candidate",
            "memory_evidence",
            "candidate_record_relation",
            "record_embedding",
            "embedding_job",
            "candidate_embedding",
            "candidate_embedding_job",
            "import_checkpoint",
            "memory_type",
            "memory_schema_version",
        )
        authority_tables = ("profile", *sensitive_tables)
        required_functions = (
            "current_profile_id",
            "has_capability",
            "submit_change_request",
            "decide_change_request",
            "proposal_inventory_definition",
            "current_operation_policy",
            "save_import_checkpoint",
            "load_import_checkpoint",
        )
        protected_tables = (
            "profile",
            "record",
            "record_revision",
            "change_request",
            "memory_type",
            "memory_schema_version",
        )
        checks: list[str] = []
        authority_checks: list[str] = []
        with self._connection() as connection:
            role = connection.execute(
                "SELECT r.rolsuper, r.rolbypassrls FROM pg_roles AS r "
                "WHERE r.rolname = session_user"
            ).fetchone()
            if not role or role[0] or role[1]:
                authority_checks.append("runtime role is superuser or has BYPASSRLS")
            schema_owner = connection.execute(
                "SELECT n.nspowner::regrole::text = session_user "
                "FROM pg_namespace AS n WHERE n.nspname = 'durable_memory'"
            ).fetchone()
            if not schema_owner:
                checks.append("missing durable_memory schema")
            elif schema_owner[0]:
                authority_checks.append("runtime role owns the durable_memory schema")
            table_rows = connection.execute(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                "c.relowner::regrole::text = session_user "
                "FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'durable_memory' AND c.relname = ANY(%s)",
                (list(authority_tables),),
            ).fetchall()
            found = {row[0] for row in table_rows}
            for name, rls, _force_rls, owned in table_rows:
                if name in sensitive_tables and not rls:
                    checks.append(f"unsafe table configuration: {name}")
                if owned:
                    authority_checks.append(f"unsafe table configuration: {name}")
            for name in set(authority_tables) - found:
                checks.append(f"missing table: {name}")
            extensions = {
                row[0]
                for row in connection.execute(
                    "SELECT extname FROM pg_extension WHERE extname = ANY(%s)",
                    (["pgcrypto", "vector"],),
                ).fetchall()
            }
            for extension in {"pgcrypto", "vector"} - extensions:
                checks.append(f"missing extension: {extension}")
            functions = {
                row[0]
                for row in connection.execute(
                    "SELECT p.proname FROM pg_proc AS p "
                    "JOIN pg_namespace AS n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = 'durable_memory' AND p.proname = ANY(%s)",
                    (list(required_functions),),
                ).fetchall()
            }
            for function in set(required_functions) - functions:
                checks.append(f"missing function: {function}")
            if not connection.execute(
                "SELECT has_schema_privilege(session_user, 'durable_memory', 'USAGE')"
            ).fetchone()[0]:
                checks.append("runtime role lacks schema usage")
            grant_rows = connection.execute(
                "SELECT has_table_privilege(session_user, 'durable_memory.record', 'SELECT'), "
                "has_table_privilege(session_user, 'durable_memory.memory_candidate', 'INSERT'), "
                "has_table_privilege(session_user, 'durable_memory.memory_type', 'SELECT'), "
                "has_function_privilege(session_user, 'durable_memory.submit_change_request(uuid, uuid, text, text, text, jsonb, text, integer, timestamptz, timestamptz, text, text)', 'EXECUTE'), "
                "COALESCE(has_function_privilege(session_user, "
                "to_regprocedure('durable_memory.save_import_checkpoint(text, text, text, jsonb)'), "
                "'EXECUTE'), false), "
                "COALESCE(has_function_privilege(session_user, "
                "to_regprocedure('durable_memory.load_import_checkpoint(text, text)'), "
                "'EXECUTE'), false), "
                "NOT has_function_privilege(session_user, 'durable_memory.apply_change_request(uuid, text, boolean)', 'EXECUTE')"
            ).fetchone()
            for label, granted in zip(
                (
                    "record SELECT",
                    "memory_candidate INSERT",
                    "memory_type SELECT",
                    "submit_change_request EXECUTE",
                    "save_import_checkpoint EXECUTE",
                    "load_import_checkpoint EXECUTE",
                    "apply_change_request EXECUTE revoked",
                ),
                grant_rows,
                strict=True,
            ):
                if not granted:
                    target = (
                        authority_checks
                        if label == "apply_change_request EXECUTE revoked"
                        else checks
                    )
                    target.append(f"runtime role lacks {label}")
            for table in protected_tables:
                for privilege in ("INSERT", "UPDATE", "DELETE"):
                    granted = connection.execute(
                        "SELECT has_table_privilege(session_user, %s, %s)",
                        (self._table_reference(table), privilege),
                    ).fetchone()[0]
                    if granted:
                        authority_checks.append(
                            f"runtime role has forbidden {privilege} on {table}"
                        )
            if connection.execute(
                "SELECT has_function_privilege(session_user, "
                "'durable_memory.auto_apply_change_request(uuid)', 'EXECUTE')"
            ).fetchone()[0]:
                authority_checks.append(
                    "runtime role has forbidden EXECUTE on auto_apply_change_request"
                )
        if not allow_unsafe_runtime:
            checks.extend(authority_checks)
        return {
            "applicable": True,
            "ok": not checks,
            "checks": checks,
            "unsafe_runtime": allow_unsafe_runtime,
            "warnings": authority_checks if allow_unsafe_runtime else [],
        }

    def operation_policy(self) -> ApprovalPolicy:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT operation, action, ttl_seconds "
                "FROM durable_memory.current_operation_policy()"
            ).fetchall()
        actions = {row[0]: row[1] for row in rows}
        ttl_values = {row[2] for row in rows}
        if set(actions) != OPERATIONS or len(ttl_values) != 1:
            raise CommandError("PostgreSQL operation policy is incomplete.")
        return ApprovalPolicy(
            create=actions["create"],
            update=actions["update"],
            delete=actions["delete"],
            ttl_seconds=ttl_values.pop(),
        )

    def pending_embedding_jobs(
        self, *, profile: Profile, limit: int
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            connection.execute(
                "WITH recovered AS (UPDATE durable_memory.embedding_job "
                "SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'pending' END, "
                "last_error = CASE WHEN attempts >= max_attempts THEN "
                "'embedding lease expired' ELSE NULL END, "
                "failed_at = CASE WHEN attempts >= max_attempts THEN now() ELSE NULL END, "
                "claim_token = NULL, claimed_at = NULL, lease_expires_at = NULL "
                "WHERE status = 'processing' "
                "AND (lease_expires_at IS NULL OR lease_expires_at < now()) "
                "RETURNING record_id, revision, status, last_error) "
                "UPDATE durable_memory.record_embedding AS projection "
                "SET lifecycle_status = recovered.status, error_message = recovered.last_error, "
                "failed_at = CASE WHEN recovered.status = 'failed' THEN now() ELSE NULL END "
                "FROM recovered WHERE (projection.record_id, projection.revision) = "
                "(recovered.record_id, recovered.revision)"
            )
            rows = connection.execute(
                "WITH claimed AS (SELECT job.record_id, job.revision "
                "FROM durable_memory.embedding_job AS job "
                "JOIN durable_memory.record_embedding AS projection "
                "ON (projection.record_id, projection.revision) = "
                "(job.record_id, job.revision) "
                "JOIN durable_memory.record AS record ON record.id = job.record_id "
                "WHERE job.status = 'pending' AND job.attempts < job.max_attempts "
                "AND record.status = 'active' "
                "AND record.valid_from <= now() "
                "AND (record.valid_to IS NULL OR record.valid_to > now()) "
                "AND record.record_type <> '__inventory_definition__' "
                "AND record.revision = job.revision "
                "ORDER BY job.created_at, job.record_id LIMIT %s FOR UPDATE OF job SKIP LOCKED) "
                "UPDATE durable_memory.embedding_job AS job SET status = 'processing', "
                "attempts = attempts + 1, claim_token = gen_random_uuid(), claimed_at = now(), "
                "lease_expires_at = now() + interval '15 minutes' FROM claimed "
                "JOIN durable_memory.record_embedding AS projection ON "
                "(projection.record_id, projection.revision) = (claimed.record_id, claimed.revision) "
                "JOIN durable_memory.record AS record ON record.id = claimed.record_id "
                "WHERE (job.record_id, job.revision) = (claimed.record_id, claimed.revision) "
                "RETURNING job.record_id::text, job.revision, projection.content_hash, "
                "record.search_text, job.claim_token::text",
                (limit,),
            ).fetchall()
        return [
            {
                "record_id": str(row[0]),
                "revision": row[1],
                "content_hash": row[2],
                "text": row[3],
                "claim_token": row[4],
            }
            for row in rows
        ]

    def complete_embedding_job(
        self,
        *,
        record_id: str,
        revision: int,
        content_hash: str,
        claim_token: str = "",
        model_identifier: str,
        vector: list[float],
    ) -> bool:
        if not model_identifier or not re.fullmatch(r"[0-9a-f]{64}", content_hash):
            raise CommandError("Embedding model and SHA-256 content hash are required.")
        values = validate_embedding(vector)
        literal = "[" + ",".join(str(value) for value in values) + "]"
        with self._connection() as connection:
            updated = connection.execute(
                "UPDATE durable_memory.record_embedding SET model_identifier = %s, "
                "dimension = %s, embedding = %s::vector, lifecycle_status = 'indexed', "
                "error_message = NULL, indexed_at = now(), failed_at = NULL "
                "WHERE record_id = %s AND revision = %s AND content_hash = %s "
                "AND EXISTS (SELECT 1 FROM durable_memory.embedding_job WHERE "
                "record_id = %s AND revision = %s AND status = 'processing' "
                "AND claim_token = %s::uuid)",
                (
                    model_identifier,
                    len(values),
                    literal,
                    record_id,
                    revision,
                    content_hash,
                    record_id,
                    revision,
                    claim_token,
                ),
            ).rowcount
            if updated:
                connection.execute(
                    "UPDATE durable_memory.embedding_job SET status = 'completed', "
                    "completed_at = now(), last_error = NULL, claim_token = NULL, "
                    "claimed_at = NULL, lease_expires_at = NULL WHERE record_id = %s "
                    "AND revision = %s AND claim_token = %s::uuid",
                    (record_id, revision, claim_token),
                )
            return updated == 1

    def fail_embedding_job(
        self, *, record_id: str, revision: int, error: str, claim_token: str = ""
    ) -> bool:
        with self._connection() as connection:
            connection.execute(
                "UPDATE durable_memory.record_embedding "
                "SET lifecycle_status = 'failed', error_message = %s, "
                "failed_at = now() WHERE record_id = %s AND revision = %s AND EXISTS "
                "(SELECT 1 FROM durable_memory.embedding_job WHERE record_id = %s "
                "AND revision = %s AND status = 'processing' AND claim_token = %s::uuid)",
                (error[:500], record_id, revision, record_id, revision, claim_token),
            )
            connection.execute(
                "UPDATE durable_memory.embedding_job SET status = 'failed', "
                "last_error = %s, failed_at = now(), claim_token = NULL, claimed_at = NULL, "
                "lease_expires_at = NULL WHERE record_id = %s "
                "AND revision = %s AND status = 'processing' AND claim_token = %s::uuid",
                (error[:500], record_id, revision, claim_token),
            )
        return True

    def requeue_failed_embedding_jobs(self, *, profile: Profile, limit: int) -> int:
        """Make a bounded, explicit retry set without discarding failure history."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT job.record_id, job.revision, job.status, job.attempts, "
                "job.max_attempts, job.last_error FROM "
                "durable_memory.embedding_job AS job "
                "JOIN durable_memory.record AS record ON record.id = job.record_id "
                "WHERE (job.status = 'failed' "
                "OR (job.status = 'processing' "
                "AND (job.lease_expires_at IS NULL OR job.lease_expires_at < now()))) "
                "AND record.status = 'active' "
                "AND record.valid_from <= now() "
                "AND (record.valid_to IS NULL OR record.valid_to > now()) "
                "AND record.record_type <> '__inventory_definition__' "
                "AND record.revision = job.revision "
                "ORDER BY COALESCE(job.failed_at, job.lease_expires_at), job.record_id "
                "LIMIT %s FOR UPDATE OF job",
                (limit,),
            ).fetchall()
            requeued = 0
            for record_id, revision, status, attempts, max_attempts, last_error in rows:
                explicit_retry = status == "failed"
                exhausted = status == "processing" and attempts >= max_attempts
                next_status = "failed" if exhausted else "pending"
                next_attempts = 0 if explicit_retry else attempts
                diagnostic = (
                    "embedding lease expired"
                    if exhausted
                    else last_error
                    if status == "failed"
                    else None
                )
                connection.execute(
                    "UPDATE durable_memory.embedding_job SET status = %s, attempts = %s, "
                    "last_error = %s, failed_at = CASE WHEN %s = 'failed' "
                    "THEN now() ELSE NULL END, claim_token = NULL, claimed_at = NULL, "
                    "lease_expires_at = NULL "
                    "WHERE record_id = %s AND revision = %s",
                    (
                        next_status,
                        next_attempts,
                        diagnostic,
                        next_status,
                        record_id,
                        revision,
                    ),
                )
                connection.execute(
                    "UPDATE durable_memory.record_embedding "
                    "SET lifecycle_status = %s, error_message = %s, "
                    "failed_at = CASE WHEN %s = 'failed' THEN now() ELSE NULL END "
                    "WHERE record_id = %s "
                    "AND revision = %s",
                    (next_status, diagnostic, next_status, record_id, revision),
                )
                if next_status == "pending":
                    requeued += 1
        return requeued

    def pending_candidate_embedding_jobs(
        self, *, profile: Profile, limit: int
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            connection.execute(
                "WITH recovered AS (UPDATE durable_memory.candidate_embedding_job "
                "SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'pending' END, "
                "last_error = CASE WHEN attempts >= max_attempts THEN "
                "'candidate embedding lease expired' ELSE NULL END, "
                "failed_at = CASE WHEN attempts >= max_attempts THEN now() ELSE NULL END, "
                "claim_token = NULL, claimed_at = NULL, lease_expires_at = NULL "
                "WHERE status = 'processing' "
                "AND (lease_expires_at IS NULL OR lease_expires_at < now()) "
                "RETURNING candidate_id, status, last_error) "
                "UPDATE durable_memory.candidate_embedding AS projection "
                "SET lifecycle_status = recovered.status, error_message = recovered.last_error, "
                "failed_at = CASE WHEN recovered.status = 'failed' THEN now() ELSE NULL END "
                "FROM recovered WHERE projection.candidate_id = recovered.candidate_id"
            )
            rows = connection.execute(
                "WITH claimed AS (SELECT job.candidate_id "
                "FROM durable_memory.candidate_embedding_job AS job "
                "JOIN durable_memory.candidate_embedding AS projection "
                "ON projection.candidate_id = job.candidate_id "
                "JOIN durable_memory.memory_candidate AS candidate "
                "ON candidate.id = job.candidate_id "
                "WHERE job.status = 'pending' AND job.attempts < job.max_attempts "
                "AND candidate.assessment = 'new' "
                "AND projection.lifecycle_status = 'pending' "
                "AND candidate.submitted_by_profile_id = durable_memory.current_profile_id() "
                "ORDER BY job.created_at, job.candidate_id "
                "LIMIT %s FOR UPDATE OF job SKIP LOCKED) "
                "UPDATE durable_memory.candidate_embedding_job AS job "
                "SET status = 'processing', attempts = attempts + 1, claim_token = gen_random_uuid(), "
                "claimed_at = now(), lease_expires_at = now() + interval '15 minutes' "
                "FROM claimed JOIN durable_memory.memory_candidate AS candidate "
                "ON candidate.id = claimed.candidate_id "
                "WHERE job.candidate_id = claimed.candidate_id "
                "RETURNING job.candidate_id::text, candidate.canonical_search_text, "
                "job.claim_token::text",
                (limit,),
            ).fetchall()
        return [
            {"candidate_id": str(row[0]), "text": row[1], "claim_token": row[2]}
            for row in rows
        ]

    def complete_candidate_embedding_job(
        self,
        *,
        candidate_id: str,
        claim_token: str,
        model_identifier: str,
        vector: list[float],
    ) -> bool:
        if not model_identifier:
            raise CommandError("Embedding model is required.")
        values = validate_embedding(vector)
        literal = "[" + ",".join(str(value) for value in values) + "]"
        with self._connection() as connection:
            updated = connection.execute(
                "UPDATE durable_memory.candidate_embedding SET model_identifier = %s, "
                "dimension = %s, embedding = %s::vector, lifecycle_status = 'indexed', "
                "error_message = NULL, indexed_at = now(), failed_at = NULL "
                "WHERE candidate_id = %s AND EXISTS (SELECT 1 FROM "
                "durable_memory.candidate_embedding_job WHERE candidate_id = %s "
                "AND status = 'processing' AND claim_token = %s::uuid)",
                (
                    model_identifier,
                    len(values),
                    literal,
                    candidate_id,
                    candidate_id,
                    claim_token,
                ),
            ).rowcount
            if updated:
                connection.execute(
                    "SELECT durable_memory.candidate_semantic_assessment(%s)",
                    (candidate_id,),
                )
                candidate_row = connection.execute(
                    "SELECT candidate.namespace_id, candidate.record_type, "
                    "candidate.identity_key, candidate.canonical_payload, "
                    "candidate.canonical_search_text FROM durable_memory.memory_candidate "
                    "AS candidate JOIN durable_memory.inventory_definition AS definition "
                    "ON definition.namespace_id = candidate.namespace_id "
                    "AND definition.record_type = candidate.record_type "
                    "WHERE candidate.id = %s AND candidate.assessment = 'new' "
                    "AND candidate.change_request_id IS NULL "
                    "AND definition.semantic_assessment_required",
                    (candidate_id,),
                ).fetchone()
                if candidate_row:
                    request_id = connection.execute(
                        "SELECT durable_memory.submit_change_request("
                        "%s, NULL, 'create', %s, %s, %s::jsonb, %s, NULL, "
                        "NULL, NULL, 'patch', %s)",
                        (
                            candidate_row[0],
                            candidate_row[1],
                            candidate_row[2],
                            json.dumps(candidate_row[3]),
                            candidate_row[4],
                            f"candidate:{candidate_id}",
                        ),
                    ).fetchone()[0]
                    connection.execute(
                        "UPDATE durable_memory.memory_candidate SET change_request_id = %s "
                        "WHERE id = %s AND change_request_id IS NULL",
                        (request_id, candidate_id),
                    )
                connection.execute(
                    "UPDATE durable_memory.candidate_embedding_job "
                    "SET status = 'completed', completed_at = now(), last_error = NULL, "
                    "claim_token = NULL, claimed_at = NULL, lease_expires_at = NULL "
                    "WHERE candidate_id = %s AND status = 'processing' "
                    "AND claim_token = %s::uuid",
                    (candidate_id, claim_token),
                )
            return updated == 1

    def fail_candidate_embedding_job(
        self, *, candidate_id: str, claim_token: str, error: str
    ) -> bool:
        with self._connection() as connection:
            connection.execute(
                "UPDATE durable_memory.candidate_embedding "
                "SET lifecycle_status = 'failed', "
                "error_message = %s, failed_at = now() WHERE candidate_id = %s "
                "AND EXISTS (SELECT 1 FROM durable_memory.candidate_embedding_job "
                "WHERE candidate_id = %s AND status = 'processing' "
                "AND claim_token = %s::uuid)",
                (error[:500], candidate_id, candidate_id, claim_token),
            )
            connection.execute(
                "UPDATE durable_memory.candidate_embedding_job SET status = 'failed', "
                "last_error = %s, failed_at = now(), claim_token = NULL, claimed_at = NULL, "
                "lease_expires_at = NULL WHERE candidate_id = %s "
                "AND status = 'processing' AND claim_token = %s::uuid",
                (error[:500], candidate_id, claim_token),
            )
        return True

    def assess_candidate_semantics(self, *, candidate_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "SELECT durable_memory.candidate_semantic_assessment(%s)",
                (candidate_id,),
            )

    def requeue_failed_candidate_embedding_jobs(
        self, *, profile: Profile, limit: int
    ) -> int:
        """Make failed candidate assessments retryable without clearing diagnostics."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT job.candidate_id, job.status, job.attempts, job.max_attempts, "
                "job.last_error FROM "
                "durable_memory.candidate_embedding_job AS job "
                "JOIN durable_memory.memory_candidate AS candidate "
                "ON candidate.id = job.candidate_id "
                "WHERE (job.status = 'failed' OR (job.status = 'processing' "
                "AND (job.lease_expires_at IS NULL OR job.lease_expires_at < now()))) "
                "AND candidate.assessment = 'new' "
                "ORDER BY COALESCE(job.failed_at, job.lease_expires_at), job.candidate_id "
                "LIMIT %s FOR UPDATE OF job",
                (limit,),
            ).fetchall()
            requeued = 0
            for candidate_id, status, attempts, max_attempts, last_error in rows:
                explicit_retry = status == "failed"
                exhausted = status == "processing" and attempts >= max_attempts
                next_status = "failed" if exhausted else "pending"
                next_attempts = 0 if explicit_retry else attempts
                diagnostic = (
                    "candidate embedding lease expired"
                    if exhausted
                    else last_error
                    if explicit_retry
                    else None
                )
                connection.execute(
                    "UPDATE durable_memory.candidate_embedding_job "
                    "SET status = %s, attempts = %s, last_error = %s, "
                    "failed_at = CASE WHEN %s = 'failed' THEN now() ELSE NULL END, "
                    "claim_token = NULL, claimed_at = NULL, lease_expires_at = NULL "
                    "WHERE candidate_id = %s",
                    (next_status, next_attempts, diagnostic, next_status, candidate_id),
                )
                connection.execute(
                    "UPDATE durable_memory.candidate_embedding "
                    "SET lifecycle_status = %s, error_message = %s, "
                    "failed_at = CASE WHEN %s = 'failed' THEN now() ELSE NULL END "
                    "WHERE candidate_id = %s",
                    (next_status, diagnostic, next_status, candidate_id),
                )
                if next_status == "pending":
                    requeued += 1
        return requeued

    def _submit_change_request(
        self,
        connection,
        *,
        actor: Profile,
        namespace: Namespace,
        operation: str,
        record_type: str,
        identity_key: str,
        search_text: str,
        payload: dict[str, Any],
        update_mode: str,
        record_id: str | None = None,
        expected_revision: int | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> ChangeRequest:
        if operation != "create":
            record_type = record_type if record_type != "fact" else ""
            identity_key = identity_key or ""
        key = idempotency_key(
            profile_id=actor.id,
            operation=operation,
            namespace_id=namespace.id,
            record_id=record_id,
            record_type=record_type,
            identity_key=identity_key,
            payload=payload,
            search_text=search_text,
            expected_revision=expected_revision,
            update_mode=update_mode,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        request_id = connection.execute(
            "SELECT durable_memory.submit_change_request(%s, %s, %s, %s, %s, "
            "%s::jsonb, %s, %s, %s, %s, %s, %s)",
            (
                namespace.id,
                record_id,
                operation,
                record_type,
                identity_key,
                json.dumps(payload),
                search_text,
                expected_revision,
                valid_from,
                valid_to,
                update_mode,
                key,
            ),
        ).fetchone()[0]
        row = connection.execute(
            self._request_sql("WHERE id = %s"), (request_id,)
        ).fetchone()
        return self._request(row)

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
        update_mode: str = "patch",
        record_id: str | None = None,
        expected_revision: int | None = None,
        inventory_definition: bool = False,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> ChangeRequest:
        if operation not in OPERATIONS:
            raise CommandError(t("unknown_operation"))
        if record_type == INVENTORY_DEFINITION_TYPE and not inventory_definition:
            raise CommandError(t("inventory_definition_immutable"))
        self.require_capability(actor, namespace, "propose")
        with self._connection() as connection:
            return self._submit_change_request(
                connection,
                actor=actor,
                namespace=namespace,
                operation=operation,
                record_type=record_type,
                identity_key=identity_key,
                search_text=search_text,
                payload=payload,
                update_mode=update_mode,
                record_id=record_id,
                expected_revision=expected_revision,
                valid_from=valid_from,
                valid_to=valid_to,
            )

    def submit_candidate(
        self,
        *,
        actor: Profile,
        namespace: Namespace,
        candidate: MemoryCandidate,
        payload: dict[str, Any],
        search_text: str,
        policy_action: str,
        ttl_seconds: int,
    ) -> tuple[str, ChangeRequest | None, CandidateAssessment]:
        self.require_capability(actor, namespace, "propose")
        with self._connection() as connection:
            assessment_row = connection.execute(
                "SELECT record_id, assessment, reason FROM "
                "durable_memory.candidate_identity_assessment(%s, %s, %s, %s, %s)",
                (
                    namespace.id,
                    candidate.record_type,
                    candidate.identity_key,
                    json.dumps(payload),
                    search_text,
                ),
            ).fetchone()
            candidate_id = _new_id()
            assessment = CandidateAssessment(
                status=assessment_row[1],
                relation=(
                    CandidateRelation(
                        record_id=str(assessment_row[0]), reason=assessment_row[2]
                    )
                    if assessment_row[0]
                    else None
                ),
            )
            request = None
            semantic_required = connection.execute(
                "SELECT semantic_assessment_required "
                "FROM durable_memory.inventory_definition "
                "WHERE namespace_id = %s AND record_type = %s",
                (namespace.id, candidate.record_type),
            ).fetchone()
            if assessment.status == "new" and not (
                semantic_required and semantic_required[0]
            ):
                request = self._submit_change_request(
                    connection,
                    actor=actor,
                    namespace=namespace,
                    operation="create",
                    record_type=candidate.record_type,
                    identity_key=candidate.identity_key,
                    search_text=search_text,
                    payload=payload,
                    update_mode="patch",
                    valid_from=candidate.valid_from,
                    valid_to=candidate.valid_to,
                )
            connection.execute(
                "INSERT INTO durable_memory.memory_candidate "
                "(id, namespace_id, change_request_id, record_type, identity_key, "
                "payload, text, canonical_payload, canonical_search_text, assessment, "
                "submitted_by_profile_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    candidate_id,
                    namespace.id,
                    request.id if request else None,
                    candidate.record_type,
                    candidate.identity_key,
                    json.dumps(candidate.payload),
                    candidate.text,
                    json.dumps(payload),
                    search_text,
                    assessment.status,
                    actor.id,
                ),
            )
            if assessment.relation:
                connection.execute(
                    "INSERT INTO durable_memory.candidate_record_relation "
                    "(candidate_id, record_id, reason) VALUES (%s, %s, %s)",
                    (
                        candidate_id,
                        assessment.relation.record_id,
                        assessment.relation.reason,
                    ),
                )
            for evidence in candidate.evidence:
                connection.execute(
                    "INSERT INTO durable_memory.memory_evidence "
                    "(id, candidate_id, source_kind, source_ref, observed_at, "
                    "confidence, extractor_identity, extractor_version) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        _new_id(),
                        candidate_id,
                        evidence.source_kind,
                        evidence.source_ref,
                        evidence.observed_at,
                        evidence.confidence,
                        evidence.extractor_identity,
                        evidence.extractor_version,
                    ),
                )
        return candidate_id, request, assessment

    def candidate_for_consolidation(
        self, profile: Profile, candidate_id: str
    ) -> tuple[MemoryCandidate, dict[str, Any], str, Record]:
        raise CommandError(
            "Candidate consolidation is currently supported by the in-memory "
            "store only."
        )

    def consolidate_candidate(
        self,
        *,
        actor: Profile,
        candidate_id: str,
        policy_action: str,
        ttl_seconds: int,
    ) -> ChangeRequest:
        with self._connection() as connection:
            request_id = connection.execute(
                "SELECT durable_memory.consolidate_candidate(%s, %s, %s, %s)",
                (candidate_id, _new_id(), policy_action, ttl_seconds),
            ).fetchone()[0]
            row = connection.execute(
                self._request_sql("WHERE id = %s"), (request_id,)
            ).fetchone()
        if not row:
            raise CommandError(t("unknown_request"))
        return self._request(row)

    @staticmethod
    def _request_sql(condition: str = "") -> str:
        return (
            "SELECT id, namespace_id, record_id, operation, record_type, identity_key, "
            "expected_revision, update_mode, payload, search_text, idempotency_key, status, "
            "policy_action, requested_by_profile_id, decided_by_profile_id, "
            "requested_at, decided_at, expires_at, valid_from, valid_to "
            "FROM durable_memory.change_request " + condition
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
        if not require_approve_capability:
            raise CommandError("PostgreSQL auto approval is trusted database policy.")
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
