from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from typing import Any

OPERATIONS = frozenset({"create", "update", "delete"})
CAPABILITIES = frozenset({"read", "propose", "approve", "admin"})
POLICY_ACTIONS = frozenset({"require", "auto", "deny"})
NAMESPACE_KINDS = frozenset({"private", "shared"})
RECORD_STATUSES = frozenset(
    {"active", "expired", "tombstoned", "retracted", "superseded"}
)
INVENTORY_DEFINITION_TYPE = "__inventory_definition__"
FIELD_KINDS = frozenset(
    {
        "string",
        "text",
        "integer",
        "number",
        "boolean",
        "object",
        "array",
        "date",
        "datetime",
        "decimal",
        "enum",
        "reference",
        "money",
        "measurement",
    }
)
ARCHETYPES = frozenset(
    {"entity", "event", "observation", "relation", "recommendation", "collection_entry"}
)
SENSITIVITIES = frozenset({"normal", "financial", "health"})
CHANGE_STATUSES = frozenset(
    {"pending", "approved", "rejected", "expired", "superseded"}
)
CANDIDATE_ASSESSMENTS = frozenset({"new", "duplicate", "conflict"})


class CommandError(ValueError):
    """An error that is safe to display through a Hermes command."""


@dataclass(frozen=True)
class Profile:
    id: str
    slug: str


@dataclass(frozen=True)
class Namespace:
    id: str
    slug: str
    kind: str
    owner_profile_id: str


@dataclass(frozen=True)
class InventoryField:
    kind: str = "string"
    required: bool = False
    filterable: bool = False
    searchable: bool = False
    semantic: bool = False
    values: tuple[str, ...] = ()


@dataclass(frozen=True)
class InventoryDefinition:
    record_type: str
    namespace_id: str
    fields: dict[str, InventoryField]
    version: int = 1
    lifecycle_status: str = "active"
    semantic_assessment_required: bool = False
    archetype: str = "entity"
    sensitivity: str = "normal"
    mutable: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.record_type,
            "namespace_id": self.namespace_id,
            "version": self.version,
            "lifecycle_status": self.lifecycle_status,
            "semantic_assessment_required": self.semantic_assessment_required,
            "archetype": self.archetype,
            "sensitivity": self.sensitivity,
            "mutable": self.mutable,
            "fields": {
                name: {
                    "kind": field.kind,
                    "required": field.required,
                    "filterable": field.filterable,
                    "searchable": field.searchable,
                    "semantic": field.semantic,
                    "values": list(field.values),
                }
                for name, field in self.fields.items()
            },
        }


@dataclass(frozen=True)
class MemoryEvidence:
    source_kind: str
    source_ref: str
    observed_at: datetime
    confidence: float
    extractor_identity: str | None = None
    extractor_version: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_kind, str)
            or not isinstance(self.source_ref, str)
            or not self.source_kind
            or not self.source_ref
        ):
            raise ValueError("Evidence source kind and reference are required.")
        if (
            not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
        ):
            raise ValueError("Evidence observed_at must include a timezone.")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not isfinite(self.confidence)
            or not 0 <= self.confidence <= 1
        ):
            raise ValueError("Evidence confidence must be between 0 and 1.")
        if (
            self.extractor_identity is not None
            and (
                not isinstance(self.extractor_identity, str)
                or not self.extractor_identity
            )
        ) or (
            self.extractor_version is not None
            and (
                not isinstance(self.extractor_version, str)
                or not self.extractor_version
            )
        ):
            raise ValueError("Evidence extractor fields cannot be empty.")


@dataclass(frozen=True)
class MemoryCandidate:
    record_type: str
    identity_key: str
    payload: dict[str, Any]
    text: str = ""
    namespace: str | None = None
    evidence: tuple[MemoryEvidence, ...] = ()
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.record_type, str)
            or not isinstance(self.identity_key, str)
            or not self.record_type
            or not self.identity_key
        ):
            raise ValueError("Candidate type and identity are required.")
        if not isinstance(self.payload, dict):
            raise ValueError("Candidate payload must be an object.")
        if not isinstance(self.text, str):
            raise ValueError("Candidate text must be a string.")
        if self.namespace is not None and (
            not isinstance(self.namespace, str) or not self.namespace
        ):
            raise ValueError("Candidate namespace cannot be empty.")
        if (
            not isinstance(self.evidence, tuple)
            or not self.evidence
            or not all(isinstance(item, MemoryEvidence) for item in self.evidence)
        ):
            raise ValueError(
                "Candidate evidence must be a non-empty tuple of MemoryEvidence."
            )
        try:
            json.dumps(self.payload, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("Candidate payload must be JSON serializable.") from error
        for name, value in (
            ("valid_from", self.valid_from),
            ("valid_to", self.valid_to),
        ):
            if value is not None and (
                not isinstance(value, datetime) or value.tzinfo is None
            ):
                raise ValueError(f"Candidate {name} must include a timezone.")
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("Candidate valid_to must be after valid_from.")


@dataclass(frozen=True)
class CandidateRelation:
    record_id: str
    reason: str

    def __post_init__(self) -> None:
        if not self.record_id or not self.reason:
            raise ValueError("Candidate relation needs a record and reason.")


@dataclass(frozen=True)
class CandidateAssessment:
    status: str
    relation: CandidateRelation | None = None

    def __post_init__(self) -> None:
        if self.status not in CANDIDATE_ASSESSMENTS:
            raise ValueError(
                "Candidate assessment must be new, duplicate, or conflict."
            )
        if (self.status == "new") != (self.relation is None):
            raise ValueError("Only duplicate and conflict assessments need a relation.")

    def as_dict(self) -> dict[str, str | None]:
        return {
            "assessment": self.status,
            "matched_record_id": self.relation.record_id if self.relation else None,
            "reason": self.relation.reason if self.relation else None,
        }


@dataclass(frozen=True)
class Record:
    id: str
    namespace_id: str
    record_type: str
    identity_key: str
    status: str
    revision: int
    search_text: str
    payload: dict[str, Any]
    origin: str
    created_by_profile_id: str
    updated_by_profile_id: str
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "namespace_id": self.namespace_id,
            "type": self.record_type,
            "identity": self.identity_key,
            "status": self.status,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "revision": self.revision,
            "text": self.search_text,
            "payload": self.payload,
        }


@dataclass
class ChangeRequest:
    id: str
    namespace_id: str
    record_id: str | None
    operation: str
    record_type: str
    identity_key: str
    expected_revision: int | None
    update_mode: str
    payload: dict[str, Any]
    search_text: str
    idempotency_key: str
    status: str
    policy_action: str
    requested_by_profile_id: str
    decided_by_profile_id: str | None = None
    requested_at: str = ""
    decided_at: str | None = None
    expires_at: str = ""
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "namespace_id": self.namespace_id,
            "record_id": self.record_id,
            "operation": self.operation,
            "type": self.record_type,
            "identity": self.identity_key,
            "expected_revision": self.expected_revision,
            "update_mode": self.update_mode,
            "text": self.search_text,
            "payload": self.payload,
            "status": self.status,
            "policy_action": self.policy_action,
            "requested_by_profile_id": self.requested_by_profile_id,
            "decided_by_profile_id": self.decided_by_profile_id,
            "expires_at": self.expires_at,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
        }


@dataclass
class Grant:
    namespace_id: str
    grantee_profile_id: str
    capability: str
    granted_by_profile_id: str


@dataclass
class StoreState:
    profiles: dict[str, Profile] = field(default_factory=dict)
    profiles_by_slug: dict[str, str] = field(default_factory=dict)
    namespaces: dict[str, Namespace] = field(default_factory=dict)
    namespaces_by_slug: dict[str, str] = field(default_factory=dict)
    grants: list[Grant] = field(default_factory=list)
    records: dict[str, Record] = field(default_factory=dict)
    inventory_definitions: dict[tuple[str, str], InventoryDefinition] = field(
        default_factory=dict
    )
    requests: dict[str, ChangeRequest] = field(default_factory=dict)
    requests_by_key: dict[str, str] = field(default_factory=dict)
    candidates: dict[str, MemoryCandidate] = field(default_factory=dict)
    candidate_request_ids: dict[str, str] = field(default_factory=dict)
    candidate_namespaces: dict[str, str] = field(default_factory=dict)
    candidate_submitters: dict[str, str] = field(default_factory=dict)
    candidate_payloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    candidate_search_texts: dict[str, str] = field(default_factory=dict)
    candidate_assessments: dict[str, CandidateAssessment] = field(default_factory=dict)
    candidate_consolidation_request_ids: dict[str, str] = field(default_factory=dict)
    namespace_retention_seconds: dict[str, int | None] = field(default_factory=dict)
    hard_purge_requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    hard_purge_audit: list[dict[str, Any]] = field(default_factory=list)
    importer_checkpoints: dict[tuple[str, str], dict[str, Any]] = field(
        default_factory=dict
    )
