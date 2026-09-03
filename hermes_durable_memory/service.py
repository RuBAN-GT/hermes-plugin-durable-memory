from __future__ import annotations

import json
import shlex
from numbers import Number
from typing import Any

from .config import Settings
from .i18n import t
from .migrations import DatabaseMigrator
from .models import (
    FIELD_KINDS,
    INVENTORY_DEFINITION_TYPE,
    NAMESPACE_KINDS,
    ChangeRequest,
    CommandError,
    InventoryDefinition,
    InventoryField,
    Namespace,
    Record,
)
from .store import InMemoryStore, PostgresStore


class DurableMemory:
    """Transport-independent memory commands and approval workflow."""

    def __init__(
        self,
        settings: Settings | None = None,
        store: InMemoryStore | PostgresStore | None = None,
        environment: dict[str, str] | None = None,
    ) -> None:
        self._environment = environment
        self._settings = settings
        self._store = store

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = Settings.from_env(self._environment)
        return self._settings

    def store(self) -> InMemoryStore | PostgresStore:
        if self._store is not None:
            return self._store
        if self.settings.store == "postgres":
            if not self.settings.database_url:
                raise CommandError(t("database_url_missing"))
            self._store = PostgresStore(self.settings.database_url)
        else:
            self._store = InMemoryStore()
        return self._store

    def execute(self, raw_args: str) -> str:
        return str(self.execute_payload(raw_args)["message"])

    def execute_payload(self, raw_args: str) -> dict[str, Any]:
        try:
            tokens = shlex.split(raw_args)
        except ValueError as error:
            raise CommandError(t("invalid_args", error=error)) from error
        if tokens and tokens[0] in {"durable-memory", "durable_memory"}:
            tokens.pop(0)
        if not tokens or tokens[0] in {"help", "--help", "-h"}:
            return {"message": t("usage")}
        command = tokens.pop(0)
        options = self._options(tokens)
        return self._dispatch(command, options)

    def doctor(self) -> dict[str, Any]:
        message = t(
            "doctor",
            store=t(f"store_{self.settings.store}"),
            profile=self.settings.profile,
            create=self.settings.policy.create,
            update=self.settings.policy.update,
            delete=self.settings.policy.delete,
            postgres=t(
                "postgres_connected"
                if self.settings.store == "postgres"
                else "postgres_not_connected"
            ),
        )
        if self.settings.store == "memory":
            message = f"{message}\n{t('doctor_ephemeral')}"
        return {
            "message": message,
            "store": self.settings.store,
            "profile": self.settings.profile,
            "policy": self.settings.policy.as_dict(),
            "postgres_ready": self.settings.store == "postgres",
            "ephemeral": self.settings.store == "memory",
        }

    def search(
        self,
        query: str = "",
        namespace_slug: str | None = None,
        record_type: str | None = None,
        filters: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        store, profile, _private = self._context()
        namespace = (
            self._visible_namespace(store, profile, namespace_slug)
            if namespace_slug
            else None
        )
        records = store.search(profile=profile, query=query, namespace=namespace)
        parsed_filters = self._json_object(filters, "filters")
        definition = (
            self._definition(store, profile, namespace, record_type)
            if record_type
            else None
        )
        if record_type:
            records = [item for item in records if item.record_type == record_type]
        if parsed_filters:
            records = [
                item
                for item in records
                if self._matches_filters(item, parsed_filters, definition)
            ]
        return {
            "message": self._search_message(query, records),
            "query": query,
            "records": [record.as_dict() for record in records],
        }

    def decide(self, request_id: str, decision: str) -> dict[str, Any]:
        """Resolve a queued change independently of its transport adapter."""
        if decision not in {"approve", "reject"}:
            raise CommandError(t("decision_invalid"))
        return self._decide({"request-id": request_id}, decision)

    def _create_inventory(self, options: dict[str, str]) -> dict[str, Any]:
        record_type = options.get("type")
        if not record_type or not options.get("fields"):
            raise CommandError(t("usage_create_inventory"))
        fields = self._parse_fields(options["fields"])
        store, profile, private = self._context()
        namespace = self._visible_namespace(
            store, profile, options.get("namespace"), default=private
        )
        if self._definition(store, profile, namespace, record_type):
            raise CommandError(t("inventory_exists", type=record_type))
        policy_action = self.settings.policy.for_operation("create")
        if policy_action == "deny":
            raise CommandError(t("operation_denied", operation=t("operation_create")))
        request = store.propose(
            actor=profile,
            namespace=namespace,
            operation="create",
            record_type=INVENTORY_DEFINITION_TYPE,
            identity_key=record_type,
            search_text=record_type,
            payload={"identity": record_type, "fields": fields},
            policy_action=policy_action,
            ttl_seconds=self.settings.policy.ttl_seconds,
        )
        if request.policy_action == "auto" and request.status == "pending":
            request = store.decide(
                actor=profile,
                request_id=request.id,
                decision="approve",
                require_approve_capability=False,
            )
        result = request.as_dict()
        result["inventory"] = record_type
        result["fields"] = fields
        result["message"] = self._proposal_message(request)
        return result

    def _list_inventories(self, options: dict[str, str]) -> dict[str, Any]:
        store, profile, private = self._context()
        namespace = self._visible_namespace(
            store, profile, options.get("namespace"), default=private
        )
        definitions = [
            self._definition_from_record(item)
            for item in store.search(profile=profile, query="", namespace=namespace)
            if item.record_type == INVENTORY_DEFINITION_TYPE
        ]
        return {
            "message": t("inventories_found", count=len(definitions)),
            "inventories": [item.as_dict() for item in definitions],
        }

    def _definition(
        self, store, profile, namespace, record_type: str
    ) -> InventoryDefinition | None:
        for item in store.search(profile=profile, query="", namespace=namespace):
            if (
                item.record_type == INVENTORY_DEFINITION_TYPE
                and item.identity_key == record_type
            ):
                return self._definition_from_record(item)
        return None

    @staticmethod
    def _definition_from_record(record: Record) -> InventoryDefinition:
        fields = {
            name: InventoryField(**spec)
            for name, spec in record.payload.get("fields", {}).items()
        }
        return InventoryDefinition(record.identity_key, record.namespace_id, fields)

    @staticmethod
    def _parse_fields(value: str) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as error:
            raise CommandError(t("fields_json_invalid")) from error
        if not isinstance(raw, dict) or not raw:
            raise CommandError(t("fields_json_object"))
        result = {}
        for name, spec in raw.items():
            if not isinstance(name, str) or not isinstance(spec, dict):
                raise CommandError(t("inventory_field_invalid"))
            kind = spec.get("kind", "string")
            if kind not in FIELD_KINDS:
                raise CommandError(t("inventory_field_kind", kind=kind))
            result[name] = {
                key: spec.get(key, False)
                for key in ("kind", "required", "filterable", "searchable", "semantic")
            }
        return result

    @staticmethod
    def _json_object(value: dict[str, Any] | str | None, name: str) -> dict[str, Any]:
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as error:
                raise CommandError(t("json_invalid", name=name)) from error
        if not isinstance(value, dict):
            raise CommandError(t("json_object", name=name))
        return value

    def _payload_for(
        self,
        options: dict[str, str],
        definition: InventoryDefinition | None,
        identity: str,
    ) -> tuple[dict[str, Any], str]:
        text = options.get("text", "")
        payload = (
            self._json_object(options.get("payload"), "payload")
            if options.get("payload")
            else {}
        )
        if not payload:
            payload = {"text": text, "identity": identity}
        if definition:
            self._validate_payload(
                payload,
                definition,
                require_required=options.get("operation") == "create",
            )
            if not text:
                text = " ".join(
                    value
                    if isinstance(value := payload[name], str)
                    else json.dumps(value, ensure_ascii=False, sort_keys=True)
                    for name, field in sorted(definition.fields.items())
                    if name in payload and (field.searchable or field.semantic)
                )
        payload.setdefault("identity", identity)
        if text:
            payload.setdefault("text", text)
        return payload, text or identity

    @staticmethod
    def _validate_payload(
        payload: dict[str, Any], definition: InventoryDefinition, require_required: bool
    ) -> None:
        unknown = set(payload) - set(definition.fields) - {"identity", "text"}
        if unknown:
            raise CommandError(t("payload_unknown", fields=", ".join(sorted(unknown))))
        for name, field in definition.fields.items():
            if name not in payload:
                if require_required and field.required:
                    raise CommandError(t("payload_required", name=name))
                continue
            value = payload[name]
            valid = {
                "string": isinstance(value, str),
                "text": isinstance(value, str),
                "integer": isinstance(value, int) and not isinstance(value, bool),
                "number": isinstance(value, Number) and not isinstance(value, bool),
                "boolean": isinstance(value, bool),
                "object": isinstance(value, dict),
                "array": isinstance(value, list),
            }[field.kind]
            if not valid:
                raise CommandError(t("payload_kind", name=name, kind=field.kind))

    @staticmethod
    def _matches_filters(
        record: Record, filters: dict[str, Any], definition: InventoryDefinition | None
    ) -> bool:
        if not definition:
            return False
        for name, expected in filters.items():
            field = definition.fields.get(name)
            if not field or not field.filterable:
                raise CommandError(t("filter_not_allowed", name=name))
            actual = record.payload.get(name)
            if isinstance(expected, dict):
                for operator, bound in expected.items():
                    try:
                        if operator in {"$gte", "gte"} and not actual >= bound:
                            return False
                        if operator in {"$gt", "gt"} and not actual > bound:
                            return False
                        if operator in {"$lte", "lte"} and not actual <= bound:
                            return False
                        if operator in {"$lt", "lt"} and not actual < bound:
                            return False
                    except TypeError:
                        return False
                    if operator in {"$eq", "eq"} and actual != bound:
                        return False
            elif actual != expected:
                return False
        return True

    def prefetch_text(self, query: str) -> str:
        if not query.strip():
            return ""
        payload = self.search(query)
        records = payload["records"]
        if not records:
            return ""
        lines = [t("prefetch_header")]
        for record in records[:8]:
            lines.append(f"- [{record['type']}] {record['identity']}: {record['text']}")
        return "\n".join(lines)

    def _dispatch(self, command: str, options: dict[str, str]) -> dict[str, Any]:
        if command == "doctor":
            return self.doctor()
        if command == "migrate":
            return self._migrate()
        if command == "migration-status":
            return self._migration_status()
        if command == "bootstrap-profile":
            return self._bootstrap_profile(options)
        if command == "namespaces":
            return self._namespaces()
        if command == "create-namespace":
            return self._create_namespace(options)
        if command == "create-inventory":
            return self._create_inventory(options)
        if command == "list-inventories":
            return self._list_inventories(options)
        if command == "grant":
            return self._grant(options)
        if command == "search":
            query = options.get("query", "")
            filter_value = options.get("filter") or options.get("filters")
            if not query and not filter_value and not options.get("type"):
                raise CommandError(t("usage_search"))
            return self.search(
                query, options.get("namespace"), options.get("type"), filter_value
            )
        if command == "propose":
            return self._propose(options)
        if command == "pending":
            return self._pending()
        if command == "approve":
            return self._decide(options, "approve")
        if command == "reject":
            return self._decide(options, "reject")
        raise CommandError(t("unknown_action", action=command, usage=t("usage")))

    def _migrator(self) -> DatabaseMigrator:
        return DatabaseMigrator(self.settings.migration_database_url or "")

    def _migrate(self) -> dict[str, Any]:
        applied = self._migrator().migrate()
        return {
            "message": t("migrations_applied", count=len(applied)),
            "applied": applied,
        }

    def _migration_status(self) -> dict[str, Any]:
        migrations = self._migrator().status()
        return {
            "message": t(
                "migration_status",
                applied=sum(item["status"] == "applied" for item in migrations),
                total=len(migrations),
            ),
            "migrations": migrations,
        }

    def _bootstrap_profile(self, options: dict[str, str]) -> dict[str, Any]:
        slug = options.get("slug")
        runtime_role = options.get("runtime-role")
        if not slug or not runtime_role:
            raise CommandError(t("usage_bootstrap_profile"))
        profile = self._migrator().bootstrap_profile(slug, runtime_role)
        return {
            "message": t("profile_bootstrapped", slug=slug, role=runtime_role),
            "profile": profile,
        }

    def _context(self):
        store = self.store()
        profile = store.get_or_create_profile(self.settings.profile)
        namespace = store.get_or_create_private_namespace(profile)
        return store, profile, namespace

    def _namespaces(self) -> dict[str, Any]:
        store, profile, _private = self._context()
        namespaces = store.list_namespaces(profile)
        items = [
            {
                "id": namespace.id,
                "slug": namespace.slug,
                "kind": namespace.kind,
                "owner": namespace.owner_profile_id == profile.id,
            }
            for namespace in namespaces
        ]
        if not items:
            message = t("namespaces_empty", profile=profile.slug)
        else:
            lines = [t("namespaces_header", profile=profile.slug)]
            for index, item in enumerate(items, start=1):
                lines.append(
                    t(
                        "namespaces_item",
                        index=index,
                        slug=item["slug"],
                        kind=t(f"kind_{item['kind']}"),
                        owner=t("namespaces_owner") if item["owner"] else "",
                    )
                )
            message = "\n".join(lines)
        return {
            "message": message,
            "profile": profile.slug,
            "namespaces": items,
        }

    def _create_namespace(self, options: dict[str, str]) -> dict[str, Any]:
        slug = options.get("slug")
        kind = options.get("kind", "shared")
        if not slug:
            raise CommandError(t("usage_create_namespace"))
        if kind not in NAMESPACE_KINDS:
            raise CommandError(t("namespace_kind_invalid"))
        store, profile, _private = self._context()
        namespace = store.create_namespace(owner=profile, slug=slug, kind=kind)
        return {
            "message": t(
                "namespace_created",
                kind=t(f"kind_{namespace.kind}"),
                slug=namespace.slug,
            ),
            "id": namespace.id,
            "slug": namespace.slug,
            "kind": namespace.kind,
        }

    def _grant(self, options: dict[str, str]) -> dict[str, Any]:
        namespace_slug = options.get("namespace")
        profile_slug = options.get("profile")
        capability = options.get("capability")
        if not namespace_slug or not profile_slug or not capability:
            raise CommandError(t("usage_grant"))
        store, actor, _private = self._context()
        namespace = store.get_namespace(namespace_slug)
        grantee = store.get_profile_by_slug(profile_slug)
        grant = store.grant(
            actor=actor,
            namespace=namespace,
            grantee=grantee,
            capability=capability,
        )
        return {
            "message": t(
                "grant_ok",
                capability=t(f"capability_{grant.capability}"),
                namespace=namespace.slug,
                profile=grantee.slug,
            ),
            "namespace": namespace.slug,
            "profile": grantee.slug,
            "capability": grant.capability,
        }

    def _propose(self, options: dict[str, str]) -> dict[str, Any]:
        operation = options.get("operation")
        record_type = options.get("type", "fact")
        identity = options.get("identity")
        if not operation:
            raise CommandError(t("usage_propose"))
        if operation not in {"create", "update", "delete"}:
            raise CommandError(t("unknown_operation"))
        if record_type == INVENTORY_DEFINITION_TYPE:
            raise CommandError(t("inventory_definition_immutable"))
        if operation == "create" and not identity:
            raise CommandError(t("usage_create_identity"))
        store, profile, private = self._context()
        namespace = self._visible_namespace(
            store, profile, options.get("namespace"), default=private
        )
        policy_action = self.settings.policy.for_operation(operation)
        if policy_action == "deny":
            raise CommandError(
                t("operation_denied", operation=t(f"operation_{operation}"))
            )
        definition = self._definition(store, profile, namespace, record_type)
        payload, search_text = self._payload_for(options, definition, identity or "")
        request = store.propose(
            actor=profile,
            namespace=namespace,
            operation=operation,
            record_type=record_type,
            identity_key=identity or "",
            search_text=search_text,
            payload=payload,
            policy_action=policy_action,
            ttl_seconds=self.settings.policy.ttl_seconds,
            record_id=options.get("record-id"),
            expected_revision=self._optional_int(options.get("expected-revision")),
        )
        if policy_action == "auto" and request.status == "pending":
            request = store.decide(
                actor=profile,
                request_id=request.id,
                decision="approve",
                require_approve_capability=False,
            )
        result = request.as_dict()
        result["message"] = self._proposal_message(request)
        return result

    def _pending(self) -> dict[str, Any]:
        store, profile, _private = self._context()
        requests = store.pending(profile)
        return {
            "message": self._pending_message(requests),
            "requests": [request.as_dict() for request in requests],
        }

    def _decide(self, options: dict[str, str], decision: str) -> dict[str, Any]:
        request_id = options.get("request-id")
        if not request_id:
            raise CommandError(t("usage_decide", command=decision))
        store, profile, _private = self._context()
        request = store.decide(actor=profile, request_id=request_id, decision=decision)
        result = request.as_dict()
        result["message"] = self._decision_message(request, decision)
        return result

    def _visible_namespace(
        self,
        store: InMemoryStore,
        profile,
        slug: str | None,
        default: Namespace | None = None,
    ) -> Namespace:
        if not slug:
            if default is None:
                raise CommandError(t("namespace_required"))
            return default
        namespace = store.get_namespace(slug)
        store.require_capability(profile, namespace, "read")
        return namespace

    def _search_message(self, query: str, records: list[Record]) -> str:
        if not records:
            return t("search_empty", query=query)
        lines = [t("search_header", count=len(records), query=query)]
        for index, record in enumerate(records, start=1):
            lines.append(
                t(
                    "search_item",
                    index=index,
                    text=record.search_text,
                    type=record.record_type,
                    identity=record.identity_key,
                )
            )
        return "\n".join(lines)

    def _pending_message(self, requests: list[ChangeRequest]) -> str:
        if not requests:
            return t("pending_empty")
        lines = [t("pending_header")]
        for index, request in enumerate(requests, start=1):
            lines.append(
                t(
                    "pending_item",
                    index=index,
                    operation=t(f"operation_{request.operation}"),
                    type=request.record_type,
                    identity=request.identity_key,
                    text=request.search_text,
                    id=request.id,
                )
            )
        return "\n".join(lines)

    def _proposal_message(self, request: ChangeRequest) -> str:
        if request.status == "approved":
            return t(
                "proposed_approved",
                type=request.record_type,
                identity=request.identity_key,
            )
        return t(
            "proposed_pending",
            operation=t(f"operation_{request.operation}"),
            type=request.record_type,
            identity=request.identity_key,
            id=request.id,
        )

    def _decision_message(self, request: ChangeRequest, decision: str) -> str:
        if request.status == "expired":
            return t("decided_expired")
        if request.status == "superseded":
            return t("decided_superseded", identity=request.identity_key)
        if decision == "approve" and request.status == "approved":
            return t(
                f"decided_approved_{request.operation}",
                type=request.record_type,
                identity=request.identity_key,
            )
        if decision == "reject" and request.status == "rejected":
            return t(
                "decided_rejected",
                operation=t(f"operation_{request.operation}"),
                type=request.record_type,
                identity=request.identity_key,
            )
        return t("decided_already", status=t(f"status_{request.status}"))

    @staticmethod
    def _optional_int(value: str | None) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except ValueError as error:
            raise CommandError(t("expected_revision_int")) from error

    @staticmethod
    def _options(tokens: list[str]) -> dict[str, str]:
        options: dict[str, str] = {}
        while tokens:
            token = tokens.pop(0)
            if not token.startswith("--"):
                raise CommandError(t("unexpected_argument", token=token))
            if token in options:
                raise CommandError(t("option_duplicate", token=token))
            if not tokens or tokens[0].startswith("--"):
                raise CommandError(t("option_missing_value", token=token))
            options[token[2:]] = tokens.pop(0)
        return options
