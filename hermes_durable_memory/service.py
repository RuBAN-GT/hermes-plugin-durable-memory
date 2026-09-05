from __future__ import annotations

import json
import shlex
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from numbers import Number
from typing import Any

from .config import Settings
from .extraction import MemoryCandidateExtractor
from .i18n import t
from .migrations import DatabaseMigrator
from .models import (
    FIELD_KINDS,
    INVENTORY_DEFINITION_TYPE,
    NAMESPACE_KINDS,
    ChangeRequest,
    CommandError,
    InventoryDefinition,
    MemoryCandidate,
    Namespace,
    Record,
)
from .ollama import OllamaEmbeddingClient
from .store import InMemoryStore, PostgresStore, merge_patch


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_iso_datetime(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


class DurableMemory:
    """Transport-independent memory commands and approval workflow."""

    def __init__(
        self,
        settings: Settings | None = None,
        store: InMemoryStore | PostgresStore | None = None,
        environment: dict[str, str] | None = None,
        extractors: Iterable[MemoryCandidateExtractor] = (),
    ) -> None:
        self._environment = environment
        self._settings = settings
        self._store = store
        self._extractors = tuple(extractors)

    @property
    def extractors(self) -> tuple[MemoryCandidateExtractor, ...]:
        """Registered candidate extractors; they receive no service or store access."""
        return self._extractors

    def register_extractor(self, extractor: MemoryCandidateExtractor) -> None:
        """Register an optional extractor for providers to invoke with turn context."""
        if not callable(getattr(extractor, "extract", None)):
            raise ValueError("Memory candidate extractor must define extract().")
        self._extractors += (extractor,)

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
        connected = False
        configured_policy = self.settings.policy
        effective_policy = configured_policy
        policy_mismatch = False
        try:
            store = self.store()
            preflight = (
                store.deployment_preflight(
                    allow_unsafe_runtime=self.settings.allow_unsafe_runtime
                )
                if isinstance(store, PostgresStore)
                else store.deployment_preflight()
            )
            connected = self.settings.store == "postgres"
            if isinstance(store, PostgresStore):
                effective_policy = store.operation_policy()
                policy_mismatch = effective_policy != configured_policy
                if policy_mismatch:
                    preflight["ok"] = False
                    preflight["checks"].append(
                        "configured approval policy does not match PostgreSQL operation policy"
                    )
        except Exception:
            preflight = {
                "applicable": True,
                "ok": False,
                "checks": ["PostgreSQL connectivity check failed."],
            }
            effective_policy = None
            policy_mismatch = self.settings.store == "postgres"
        reported_policy = effective_policy or configured_policy
        message = t(
            "doctor",
            store=t(f"store_{self.settings.store}"),
            profile=self.settings.profile,
            create=reported_policy.create,
            update=reported_policy.update,
            delete=reported_policy.delete,
            postgres=t("postgres_connected" if connected else "postgres_not_connected"),
        )
        if self.settings.store == "postgres" and self.settings.allow_unsafe_runtime:
            message = f"{message}\n{t('unsafe_runtime_warning')}"
        if self.settings.store == "memory":
            message = f"{message}\n{t('doctor_ephemeral')}"
        if not preflight["ok"]:
            message = f"{message}\nDeployment preflight failed: {'; '.join(preflight['checks'])}"
        return {
            "message": message,
            "store": self.settings.store,
            "profile": self.settings.profile,
            "policy": reported_policy.as_dict(),
            "effective_policy": effective_policy.as_dict()
            if effective_policy
            else None,
            "configured_policy": configured_policy.as_dict(),
            "policy_mismatch": policy_mismatch,
            "postgres_ready": connected and bool(preflight["ok"]),
            "ephemeral": self.settings.store == "memory",
            "deployment_preflight": preflight,
        }

    def expire_records(self, limit: int = 100) -> dict[str, int]:
        """Apply a bounded lifecycle transition without deleting canonical data."""
        store, profile, _private = self._context()
        return {"affected": store.expire_records(profile=profile, limit=limit)}

    def export(self, namespace_slug: str | None = None) -> dict[str, Any]:
        """Return canonical user data without runtime roles, URLs, or secrets."""
        store, profile, private = self._context()
        if namespace_slug:
            namespaces = [self._visible_namespace(store, profile, namespace_slug)]
        else:
            namespaces = []
            for namespace in store.list_namespaces(profile):
                try:
                    store.require_capability(profile, namespace, "read")
                except CommandError:
                    continue
                namespaces.append(namespace)
            if not namespaces:
                namespaces = [private]
        result = {
            "profile": profile.slug,
            "namespaces": [
                store.export_namespace(profile=profile, namespace=namespace)
                for namespace in namespaces
            ],
        }
        result["message"] = json.dumps(result, ensure_ascii=False, sort_keys=True)
        return result

    def set_retention_policy(
        self, namespace_slug: str, retention_seconds: int | None
    ) -> dict[str, Any]:
        store, profile, _private = self._context()
        namespace = self._visible_namespace(
            store, profile, namespace_slug, capability="admin"
        )
        store.set_retention_policy(
            actor=profile, namespace=namespace, retention_seconds=retention_seconds
        )
        return {
            "message": f"Retention policy updated for {namespace.slug}.",
            "namespace": namespace.slug,
            "retention_seconds": retention_seconds,
        }

    def request_hard_purge(
        self, namespace_slug: str, record_id: str, reason: str
    ) -> dict[str, Any]:
        store, profile, _private = self._context()
        namespace = self._visible_namespace(
            store, profile, namespace_slug, capability="admin"
        )
        result = store.request_hard_purge(
            actor=profile, namespace=namespace, record_id=record_id, reason=reason
        )
        result["message"] = "Hard purge request is pending independent admin approval."
        return result

    def approve_hard_purge(self, request_id: str) -> dict[str, Any]:
        store, profile, _private = self._context()
        result = store.approve_hard_purge(actor=profile, request_id=request_id)
        result["message"] = (
            "Hard purge completed."
            if result["status"] == "purged"
            else "Hard purge request unchanged."
        )
        return result

    def search(
        self,
        query: str = "",
        namespace_slug: str | None = None,
        record_type: str | None = None,
        filters: dict[str, Any] | str | None = None,
        limit: int = 8,
        cursor: str | None = None,
        sort: str | None = None,
        descending: bool = False,
    ) -> dict[str, Any]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 50
        ):
            raise CommandError("Search limit must be between 1 and 50.")
        store, profile, _private = self._context()
        namespace = (
            self._visible_namespace(store, profile, namespace_slug)
            if namespace_slug
            else None
        )
        parsed_filters = self._json_object(filters, "filters")
        if cursor and query.strip() and not sort:
            raise CommandError(t("ranked_cursor_unsupported"))
        if (parsed_filters or sort) and not namespace:
            raise CommandError(t("schema_search_namespace_required"))
        if parsed_filters and not record_type:
            raise CommandError("Filters require an explicit record type.")
        definitions: list[InventoryDefinition] = []
        sort_kind = None
        if parsed_filters:
            namespaces = [namespace] if namespace else store.list_namespaces(profile)
            definitions = [
                definition
                for item in namespaces
                if (
                    definition := store.get_inventory_definition(
                        profile, item, record_type or ""
                    )
                )
                is not None
            ]
            if not definitions:
                raise CommandError("Filters require an active inventory definition.")
            for definition in definitions:
                self._validate_filters(parsed_filters, definition)
        if sort:
            if not record_type:
                raise CommandError("Sorting requires an explicit record type.")
            definitions = definitions or [
                definition
                for item in (
                    [namespace] if namespace else store.list_namespaces(profile)
                )
                if (
                    definition := store.get_inventory_definition(
                        profile, item, record_type
                    )
                )
            ]
            if not definitions or any(
                sort not in definition.fields or not definition.fields[sort].filterable
                for definition in definitions
            ):
                raise CommandError(
                    "Sort field is not allowed by the inventory definition."
                )
            sort_kinds = {definition.fields[sort].kind for definition in definitions}
            if len(sort_kinds) != 1:
                raise CommandError(
                    "Sort field kind must match across visible inventory definitions."
                )
            sort_kind = next(iter(sort_kinds))
        # Apply filters in the store before ranking and limiting. This prevents
        # false empty pages when a matching record ranks after unfiltered rows.
        retrieval_limit = limit
        fts_records = store.search(
            profile=profile,
            query=query,
            namespace=namespace,
            limit=retrieval_limit,
            record_type=record_type,
            filters=parsed_filters or None,
            cursor=cursor,
            sort=sort,
            sort_kind=sort_kind,
            descending=descending,
        )
        vector_records: list[tuple[Record, float]] = []
        client = OllamaEmbeddingClient.from_settings(self.settings)
        if query.strip() and client.config.enabled and isinstance(store, PostgresStore):
            query_vector = client.embed(query)
            if query_vector:
                vector_records = store.vector_search(
                    profile=profile,
                    query_vector=query_vector,
                    model_identifier=client.config.model or "",
                    namespace=namespace,
                    limit=retrieval_limit,
                    record_type=record_type,
                    filters=parsed_filters or None,
                )
                if record_type:
                    vector_records = [
                        item
                        for item in vector_records
                        if item[0].record_type == record_type
                    ]
        ranked = (
            [
                {"record": record, "score": 0.0, "source": "structured"}
                for record in fts_records
            ]
            if sort
            else self._hybrid_rank(
                fts_records,
                vector_records,
                limit,
                self.settings.embedding_max_distance,
            )
        )
        records = [item["record"] for item in ranked]
        return {
            "message": self._search_message(query, records),
            "query": query,
            "records": [record.as_dict() for record in records],
            "results": [
                {
                    **item["record"].as_dict(),
                    "score": item["score"],
                    "source": item["source"],
                }
                for item in ranked
            ],
            "next_cursor": records[-1].id if len(records) == limit else None,
        }

    def index_embeddings(self, limit: int = 8) -> dict[str, int | str]:
        """Best-effort projection worker; it never changes canonical records."""
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 50
        ):
            raise CommandError("Embedding index limit must be between 1 and 50.")
        store, profile, _private = self._context()
        client = OllamaEmbeddingClient.from_settings(self.settings)
        if not client.config.enabled or not isinstance(store, PostgresStore):
            return {"status": "disabled", "indexed": 0, "failed": 0}
        indexed = failed = 0
        for job in store.pending_embedding_jobs(profile=profile, limit=limit):
            vector = client.embed(job["text"])
            if vector is None:
                store.fail_embedding_job(
                    record_id=job["record_id"],
                    revision=job["revision"],
                    claim_token=job["claim_token"],
                    error=(
                        "embedding provider unavailable or returned an invalid vector"
                    ),
                )
                failed += 1
                continue
            try:
                completed = store.complete_embedding_job(
                    record_id=job["record_id"],
                    revision=job["revision"],
                    claim_token=job["claim_token"],
                    content_hash=job["content_hash"],
                    model_identifier=client.config.model or "",
                    vector=vector,
                )
            except CommandError:
                store.fail_embedding_job(
                    record_id=job["record_id"],
                    revision=job["revision"],
                    claim_token=job["claim_token"],
                    error="embedding vector failed validation",
                )
                failed += 1
            else:
                if completed:
                    indexed += 1
        return {"status": "ok", "indexed": indexed, "failed": failed}

    def assess_candidate_semantics(self, limit: int = 8) -> dict[str, int | str]:
        """Best-effort candidate-only semantic assessment worker."""
        self._worker_limit(limit, "Candidate assessment limit")
        store, profile, _private = self._context()
        if isinstance(store, InMemoryStore):
            return {"status": "unsupported", "assessed": 0, "failed": 0}
        client = OllamaEmbeddingClient.from_settings(self.settings)
        if not client.config.enabled:
            return {"status": "disabled", "assessed": 0, "failed": 0}
        assessed = failed = 0
        for job in store.pending_candidate_embedding_jobs(profile=profile, limit=limit):
            vector = client.embed(job["text"])
            if vector is None:
                store.fail_candidate_embedding_job(
                    candidate_id=job["candidate_id"],
                    claim_token=job["claim_token"],
                    error=(
                        "embedding provider unavailable or returned an invalid vector"
                    ),
                )
                failed += 1
                continue
            try:
                completed = store.complete_candidate_embedding_job(
                    candidate_id=job["candidate_id"],
                    claim_token=job["claim_token"],
                    model_identifier=client.config.model or "",
                    vector=vector,
                )
            except CommandError:
                store.fail_candidate_embedding_job(
                    candidate_id=job["candidate_id"],
                    claim_token=job["claim_token"],
                    error="candidate embedding vector failed validation",
                )
                failed += 1
            else:
                if completed:
                    assessed += 1
        return {"status": "ok", "assessed": assessed, "failed": failed}

    def requeue_embedding_jobs(self, limit: int = 8) -> dict[str, int | str]:
        """Explicitly retry failed rebuildable embedding projection jobs."""
        self._worker_limit(limit, "Embedding requeue limit")
        store, profile, _private = self._context()
        if isinstance(store, InMemoryStore):
            return {"status": "unsupported", "requeued": 0}
        return {
            "status": "ok",
            "requeued": store.requeue_failed_embedding_jobs(
                profile=profile, limit=limit
            ),
        }

    def requeue_candidate_embedding_jobs(self, limit: int = 8) -> dict[str, int | str]:
        """Explicitly retry failed candidate-only semantic assessment jobs."""
        self._worker_limit(limit, "Candidate embedding requeue limit")
        store, profile, _private = self._context()
        if isinstance(store, InMemoryStore):
            return {"status": "unsupported", "requeued": 0}
        return {
            "status": "ok",
            "requeued": store.requeue_failed_candidate_embedding_jobs(
                profile=profile, limit=limit
            ),
        }

    @staticmethod
    def _worker_limit(limit: int, label: str) -> None:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 50
        ):
            raise CommandError(f"{label} must be between 1 and 50.")

    @staticmethod
    def _hybrid_rank(
        fts_records: list[Record],
        vector_records: list[tuple[Record, float]],
        limit: int,
        max_distance: float = 0.35,
    ) -> list[dict[str, Any]]:
        ranked: dict[str, dict[str, Any]] = {}
        for rank, record in enumerate(fts_records, start=1):
            ranked[record.id] = {
                "record": record,
                "score": 1 / (60 + rank),
                "source": "fts",
            }
        for rank, (record, distance) in enumerate(vector_records, start=1):
            if distance > max_distance:
                continue
            score = (1 - distance / 2) / (60 + rank)
            existing = ranked.get(record.id)
            if existing:
                existing["score"] += score
                existing["source"] = "hybrid"
            else:
                ranked[record.id] = {
                    "record": record,
                    "score": score,
                    "source": "vector",
                }
        return sorted(
            ranked.values(), key=lambda item: (-item["score"], item["record"].id)
        )[:limit]

    def decide(self, request_id: str, decision: str) -> dict[str, Any]:
        """Resolve a queued change independently of its transport adapter."""
        if decision not in {"approve", "reject"}:
            raise CommandError(t("decision_invalid"))
        return self._decide({"request-id": request_id}, decision)

    def submit_candidate(self, candidate: MemoryCandidate) -> dict[str, Any]:
        """Submit skill-extracted memory through the normal approval workflow."""
        if candidate.record_type == INVENTORY_DEFINITION_TYPE:
            raise CommandError(t("inventory_definition_immutable"))
        store, profile, private = self._context()
        namespace = self._visible_namespace(
            store,
            profile,
            candidate.namespace,
            default=private,
            capability="propose",
        )
        policy_action = self._policy_action(store, "create")
        if policy_action == "deny":
            raise CommandError(t("operation_denied", operation=t("operation_create")))
        definition = store.get_inventory_definition_for_proposal(
            profile, namespace, candidate.record_type
        )
        payload, search_text = self._payload_for(
            {"payload": candidate.payload, "text": candidate.text},
            definition,
            candidate.identity_key,
        )
        candidate_id, request, assessment = store.submit_candidate(
            actor=profile,
            namespace=namespace,
            candidate=candidate,
            payload=payload,
            search_text=search_text,
            policy_action=policy_action,
            ttl_seconds=self.settings.policy.ttl_seconds,
        )
        if (
            request
            and isinstance(store, InMemoryStore)
            and policy_action == "auto"
            and request.status == "pending"
        ):
            request = store.decide(
                actor=profile,
                request_id=request.id,
                decision="approve",
                require_approve_capability=False,
            )
        result = assessment.as_dict()
        result["candidate_id"] = candidate_id
        if request:
            result.update(request.as_dict())
            result["message"] = self._proposal_message(request)
        else:
            result["message"] = t("candidate_assessed", assessment=assessment.status)
        return result

    def consolidate_candidate(self, candidate_id: str) -> dict[str, Any]:
        """Turn a reviewed duplicate or conflict into an update proposal."""
        store = self.store()
        profile = store.get_or_create_profile(self.settings.profile)
        policy_action = self._policy_action(store, "update")
        if policy_action == "deny":
            raise CommandError(t("operation_denied", operation=t("operation_update")))
        request = store.consolidate_candidate(
            actor=profile,
            candidate_id=candidate_id,
            policy_action=policy_action,
            ttl_seconds=self.settings.policy.ttl_seconds,
        )
        if (
            isinstance(store, InMemoryStore)
            and policy_action == "auto"
            and request.status == "pending"
        ):
            request = store.decide(
                actor=profile,
                request_id=request.id,
                decision="approve",
                require_approve_capability=False,
            )
        result = request.as_dict()
        result["candidate_id"] = candidate_id
        result["message"] = self._proposal_message(request)
        return result

    def _create_inventory(self, options: dict[str, str]) -> dict[str, Any]:
        record_type = options.get("type")
        if not record_type or not options.get("fields"):
            raise CommandError(t("usage_create_inventory"))
        fields = self._parse_fields(options["fields"])
        store, profile, private = self._context()
        namespace = self._visible_namespace(
            store,
            profile,
            options.get("namespace"),
            default=private,
            capability="propose",
        )
        if store.get_inventory_definition_for_proposal(profile, namespace, record_type):
            raise CommandError(t("inventory_exists", type=record_type))
        policy_action = self._policy_action(store, "create")
        if policy_action == "deny":
            raise CommandError(t("operation_denied", operation=t("operation_create")))
        request = store.propose_inventory(
            actor=profile,
            namespace=namespace,
            record_type=record_type,
            fields=fields,
            policy_action=policy_action,
            ttl_seconds=self.settings.policy.ttl_seconds,
        )
        if (
            isinstance(store, InMemoryStore)
            and request.policy_action == "auto"
            and request.status == "pending"
        ):
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
            store,
            profile,
            options.get("namespace"),
            default=private,
        )
        definitions = store.list_inventory_definitions(profile, namespace)
        return {
            "message": t("inventories_found", count=len(definitions)),
            "inventories": [item.as_dict() for item in definitions],
        }

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
            if not name or name in {"identity", "text"} or name.startswith("__"):
                raise CommandError(t("inventory_field_invalid"))
            kind = spec.get("kind", "string")
            if kind not in FIELD_KINDS:
                raise CommandError(t("inventory_field_kind", kind=kind))
            for flag in ("required", "filterable", "searchable", "semantic"):
                if flag in spec and not isinstance(spec[flag], bool):
                    raise CommandError(t("inventory_field_invalid"))
            values = spec.get("values", [])
            if kind == "enum":
                if (
                    not isinstance(values, list)
                    or not values
                    or any(not isinstance(item, str) or not item for item in values)
                    or len(set(values)) != len(values)
                ):
                    raise CommandError(t("inventory_field_invalid"))
            elif "values" in spec:
                raise CommandError(t("inventory_field_invalid"))
            result[name] = {
                key: kind if key == "kind" else spec.get(key, False)
                for key in ("kind", "required", "filterable", "searchable", "semantic")
            }
            if kind == "enum":
                result[name]["values"] = values
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
        existing: Record | None = None,
        replace: bool = False,
    ) -> tuple[dict[str, Any], str]:
        text = options.get("text", "")
        patch = (
            self._json_object(options.get("payload"), "payload")
            if options.get("payload")
            else {}
        )
        if "identity" in patch and patch["identity"] != identity:
            raise CommandError(t("identity_immutable"))
        if patch.get("identity") is None and "identity" in patch:
            raise CommandError(t("identity_immutable"))
        if text:
            patch["text"] = text
        if existing:
            payload = patch if replace else self._merge_patch(existing.payload, patch)
        else:
            payload = patch or {"text": text, "identity": identity}
        if payload.get("identity") not in (None, identity):
            raise CommandError(t("identity_immutable"))
        payload["identity"] = identity
        if definition:
            self._validate_payload(payload, definition, require_required=True)
            if not text:
                text = " ".join(
                    value
                    if isinstance(value := payload[name], str)
                    else json.dumps(value, ensure_ascii=False, sort_keys=True)
                    for name, field in sorted(definition.fields.items())
                    if name in payload and (field.searchable or field.semantic)
                )
        if text:
            payload["text"] = text
        return payload, text or identity

    @staticmethod
    def _merge_patch(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        """Apply RFC 7396-style JSON merge patch without mutating either input."""
        return merge_patch(current, patch)

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

            def decimal_string(item: Any) -> bool:
                if not isinstance(item, str):
                    return False
                try:
                    Decimal(item)
                except InvalidOperation:
                    return False
                return True

            valid = {
                "string": isinstance(value, str),
                "text": isinstance(value, str),
                "integer": isinstance(value, int) and not isinstance(value, bool),
                "number": isinstance(value, Number) and not isinstance(value, bool),
                "boolean": isinstance(value, bool),
                "object": isinstance(value, dict),
                "array": isinstance(value, list),
                "date": isinstance(value, str) and _is_iso_date(value),
                "datetime": isinstance(value, str) and _is_iso_datetime(value),
                "decimal": decimal_string(value),
                "enum": isinstance(value, str) and value in field.values,
                "reference": isinstance(value, str) and bool(value),
                "money": isinstance(value, dict)
                and isinstance(value.get("amount_minor"), int)
                and not isinstance(value.get("amount_minor"), bool)
                and isinstance(value.get("currency"), str)
                and len(value["currency"]) == 3,
                "measurement": isinstance(value, dict)
                and decimal_string(value.get("value"))
                and isinstance(value.get("unit"), str)
                and bool(value["unit"]),
            }[field.kind]
            if not valid:
                raise CommandError(t("payload_kind", name=name, kind=field.kind))

    @staticmethod
    def _validate_filters(
        filters: dict[str, Any], definition: InventoryDefinition
    ) -> None:
        for name, expected in filters.items():
            field = definition.fields.get(name)
            if not field or not field.filterable:
                raise CommandError(t("filter_not_allowed", name=name))
            operations = (
                expected.items() if isinstance(expected, dict) else (("eq", expected),)
            )
            for operator, bound in operations:
                operator = operator.removeprefix("$")
                if operator not in {
                    "eq",
                    "ne",
                    "gt",
                    "gte",
                    "lt",
                    "lte",
                    "in",
                    "contains",
                }:
                    raise CommandError(f"Unknown filter operator: {operator}.")
                if operator == "in":
                    if not isinstance(bound, list):
                        raise CommandError(
                            f"Filter in is incompatible with field {name}."
                        )
                    for item in bound:
                        DurableMemory._validate_payload({name: item}, definition, False)
                elif operator == "contains":
                    if field.kind not in {"string", "text", "array", "object"}:
                        raise CommandError(
                            f"Filter contains is incompatible with field {name}."
                        )
                    if not isinstance(bound, str) and field.kind != "array":
                        raise CommandError(
                            f"Filter contains is incompatible with field {name}."
                        )
                else:
                    DurableMemory._validate_payload({name: bound}, definition, False)

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
                    operator = operator.removeprefix("$")
                    if operator not in {
                        "eq",
                        "ne",
                        "gt",
                        "gte",
                        "lt",
                        "lte",
                        "in",
                        "contains",
                    }:
                        raise CommandError(f"Unknown filter operator: {operator}.")
                    try:
                        if operator == "gte" and not actual >= bound:
                            return False
                        if operator == "gt" and not actual > bound:
                            return False
                        if operator == "lte" and not actual <= bound:
                            return False
                        if operator == "lt" and not actual < bound:
                            return False
                        if operator == "eq" and actual != bound:
                            return False
                        if operator == "ne" and actual == bound:
                            return False
                        if operator == "in" and (
                            not isinstance(bound, list) or actual not in bound
                        ):
                            return False
                        if operator == "contains" and (
                            not isinstance(actual, (str, list, dict))
                            or bound not in actual
                        ):
                            return False
                    except TypeError:
                        raise CommandError(
                            f"Filter {operator} is incompatible with field {name}."
                        ) from None
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
        if command == "export":
            return self.export(options.get("namespace"))
        if command == "set-retention":
            namespace = options.get("namespace")
            if not namespace or "seconds" not in options:
                raise CommandError("set-retention requires --namespace and --seconds.")
            seconds = (
                None
                if options["seconds"] == "none"
                else self._optional_int(options["seconds"])
            )
            return self.set_retention_policy(namespace, seconds)
        if command == "request-hard-purge":
            if not all(
                options.get(name) for name in ("namespace", "record-id", "reason")
            ):
                raise CommandError(
                    "request-hard-purge requires --namespace, --record-id, and --reason."
                )
            return self.request_hard_purge(
                options["namespace"], options["record-id"], options["reason"]
            )
        if command == "approve-hard-purge":
            if not options.get("request-id"):
                raise CommandError("approve-hard-purge requires --request-id.")
            return self.approve_hard_purge(options["request-id"])
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
                query,
                options.get("namespace"),
                options.get("type"),
                filter_value,
                8
                if self._optional_int(options.get("limit")) is None
                else self._optional_int(options.get("limit")),
                options.get("cursor"),
                options.get("sort"),
                self._bool_option(options.get("descending")),
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
        url = self.settings.migration_database_url
        if not url and self.settings.allow_unsafe_runtime:
            url = self.settings.database_url
        return DatabaseMigrator(url or "")

    @staticmethod
    def setup_database(plan) -> dict[str, Any]:
        """Operator-only setup. No CLI, gateway, or profile-file side effects."""
        import psycopg

        from .setup_plan import provision_database

        provision_database(plan)
        # Verify both identities before migrations; exceptions are redacted by
        # the interactive adapter, never printed as raw driver diagnostics.
        for account in (plan.runtime, plan.owner):
            with psycopg.connect(account.url) as connection:
                user = connection.execute("SELECT session_user").fetchone()[0]
                if user != account.user:
                    raise CommandError(t("setup_connection_identity", language="en"))
        settings = plan.settings()
        migrator = DatabaseMigrator(plan.owner.url)
        migrator.migrate()
        migrator.bootstrap_profile(plan.profile, plan.runtime.user, settings.policy)
        if not plan.danger:
            migrator.grant_runtime(plan.runtime.user)
        result = DurableMemory(settings=settings).doctor()
        if not result["postgres_ready"]:
            raise CommandError(t("setup_preflight_failed", language="en"))
        return result

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
        profile = self._migrator().bootstrap_profile(
            slug, runtime_role, self.settings.policy
        )
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
            store,
            profile,
            options.get("namespace"),
            default=private,
            capability="propose",
        )
        policy_action = self._policy_action(store, operation)
        if policy_action == "deny":
            raise CommandError(
                t("operation_denied", operation=t(f"operation_{operation}"))
            )
        if operation != "create" and not options.get("record-id"):
            raise CommandError(t("mutation_needs_record_id"))
        existing = None
        if operation != "create":
            if isinstance(store, InMemoryStore):
                existing = store.get_record_for_proposal(
                    profile, namespace, options.get("record-id")
                )
                record_type = existing.record_type
                identity = existing.identity_key
            else:
                # PostgreSQL resolves update/delete targets inside submission.
                # This keeps propose-only callers from reading canonical payloads.
                record_type = options.get("type", "")
                identity = options.get("identity", "")
        definition = store.get_inventory_definition_for_proposal(
            profile, namespace, record_type
        )
        payload, search_text = self._payload_for(
            options,
            definition,
            identity or "",
            existing=existing,
            replace=self._bool_option(options.get("replace")),
        )
        update_mode = (
            "replace" if self._bool_option(options.get("replace")) else "patch"
        )
        if operation != "create":
            payload.pop("identity", None)
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
            update_mode=update_mode,
            record_id=options.get("record-id"),
            expected_revision=self._optional_int(options.get("expected-revision")),
        )
        if (
            isinstance(store, InMemoryStore)
            and policy_action == "auto"
            and request.status == "pending"
        ):
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

    def _policy_action(
        self, store: InMemoryStore | PostgresStore, operation: str
    ) -> str:
        configured = self.settings.policy
        if isinstance(store, PostgresStore):
            effective = store.operation_policy()
            if effective != configured:
                raise CommandError(
                    "Configured approval policy does not match PostgreSQL operation policy."
                )
            return effective.for_operation(operation)
        return configured.for_operation(operation)

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
        capability: str = "read",
    ) -> Namespace:
        if not slug:
            if default is None:
                raise CommandError(t("namespace_required"))
            return default
        namespace = store.get_namespace(slug)
        store.require_capability(profile, namespace, capability)
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
    def _bool_option(value: str | None) -> bool:
        if value is None:
            return False
        if value.lower() in {"true", "1", "yes"}:
            return True
        if value.lower() in {"false", "0", "no"}:
            return False
        raise CommandError(t("replace_bool"))

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
