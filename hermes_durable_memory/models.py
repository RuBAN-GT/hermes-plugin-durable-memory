from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

OPERATIONS = frozenset({"create", "update", "delete"})
CAPABILITIES = frozenset({"read", "propose", "approve", "admin"})
POLICY_ACTIONS = frozenset({"require", "auto", "deny"})
NAMESPACE_KINDS = frozenset({"private", "shared"})
RECORD_STATUSES = frozenset({"active", "tombstoned"})
INVENTORY_DEFINITION_TYPE = "__inventory_definition__"
FIELD_KINDS = frozenset(
    {"string", "text", "integer", "number", "boolean", "object", "array"}
)
CHANGE_STATUSES = frozenset(
    {"pending", "approved", "rejected", "expired", "superseded"}
)


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


@dataclass(frozen=True)
class InventoryDefinition:
    record_type: str
    namespace_id: str
    fields: dict[str, InventoryField]

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.record_type,
            "namespace_id": self.namespace_id,
            "fields": {
                name: {
                    "kind": field.kind,
                    "required": field.required,
                    "filterable": field.filterable,
                    "searchable": field.searchable,
                    "semantic": field.semantic,
                }
                for name, field in self.fields.items()
            },
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "namespace_id": self.namespace_id,
            "type": self.record_type,
            "identity": self.identity_key,
            "status": self.status,
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "namespace_id": self.namespace_id,
            "record_id": self.record_id,
            "operation": self.operation,
            "type": self.record_type,
            "identity": self.identity_key,
            "expected_revision": self.expected_revision,
            "text": self.search_text,
            "payload": self.payload,
            "status": self.status,
            "policy_action": self.policy_action,
            "requested_by_profile_id": self.requested_by_profile_id,
            "decided_by_profile_id": self.decided_by_profile_id,
            "expires_at": self.expires_at,
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
    requests: dict[str, ChangeRequest] = field(default_factory=dict)
    requests_by_key: dict[str, str] = field(default_factory=dict)
