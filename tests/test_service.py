from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from hermes_durable_memory.config import Settings
from hermes_durable_memory.models import (
    CommandError,
    MemoryCandidate,
    MemoryEvidence,
    Namespace,
    Profile,
)
from hermes_durable_memory.policies import ApprovalPolicy
from hermes_durable_memory.service import DurableMemory
from hermes_durable_memory.store import (
    InMemoryStore,
    PostgresStore,
    idempotency_key,
    validate_embedding,
)


def _memory(profile: str, store: InMemoryStore, **policy: str) -> DurableMemory:
    return DurableMemory(
        settings=Settings(
            store="memory",
            profile=profile,
            policy=ApprovalPolicy(**policy),
        ),
        store=store,
    )


class DurableMemoryTests(unittest.TestCase):
    def test_idempotency_fingerprint_is_structured_and_complete(self) -> None:
        # Given
        base = {
            "profile_id": "profile",
            "operation": "create",
            "namespace_id": "namespace",
            "record_id": None,
            "record_type": "person",
            "identity_key": "person:ada",
            "payload": {"name": "Ada"},
            "search_text": "Ada",
            "expected_revision": None,
            "update_mode": "patch",
        }

        # When
        keys = {
            idempotency_key(**base),
            idempotency_key(**{**base, "search_text": "Alias"}),
            idempotency_key(
                **{**base, "valid_from": datetime(2026, 1, 1, tzinfo=timezone.utc)}
            ),
            idempotency_key(**{**base, "namespace_id": "other"}),
            idempotency_key(**{**base, "payload": {"name": "Lin"}}),
            idempotency_key(**{**base, "record_type": "a|b", "identity_key": "c"}),
            idempotency_key(**{**base, "record_type": "a", "identity_key": "b|c"}),
        }

        # Then
        self.assertEqual(len(keys), 7)

    def test_in_memory_proposal_forwards_validity_to_idempotency(self) -> None:
        # Given
        store = InMemoryStore()
        profile = store.get_or_create_profile("alpha")
        namespace = store.get_or_create_private_namespace(profile)
        valid_from = datetime(2026, 1, 1, tzinfo=timezone.utc)

        # When
        first = store.propose(
            actor=profile,
            namespace=namespace,
            operation="create",
            record_type="fact",
            identity_key="validity:first",
            search_text="same",
            payload={"identity": "validity:first"},
            policy_action="require",
            ttl_seconds=60,
            valid_from=valid_from,
        )
        second = store.propose(
            actor=profile,
            namespace=namespace,
            operation="create",
            record_type="fact",
            identity_key="validity:first",
            search_text="same",
            payload={"identity": "validity:first"},
            policy_action="require",
            ttl_seconds=60,
            valid_from=valid_from + timedelta(days=1),
        )

        # Then
        self.assertEqual(
            (first.id == second.id, first.valid_from, second.valid_from),
            (False, valid_from, valid_from + timedelta(days=1)),
        )

    def test_in_memory_descending_schema_sort_keeps_nulls_last(self) -> None:
        # Given
        memory = _memory("alpha", InMemoryStore(), create="auto")
        memory.execute_payload(
            "create-inventory --type ranked --fields "
            '\'{"name":{"kind":"string","searchable":true},'
            '"priority":{"kind":"integer","filterable":true}}\''
        )
        for identity, payload in (
            ("ranked:two", '{"name":"Item","priority":2}'),
            ("ranked:null", '{"name":"Item"}'),
            ("ranked:one", '{"name":"Item","priority":1}'),
        ):
            memory.execute_payload(
                f"propose --operation create --type ranked --identity {identity} "
                f"--payload '{payload}'"
            )

        # When
        records = memory.search(
            "Item",
            namespace_slug="profile:alpha",
            record_type="ranked",
            sort="priority",
            descending=True,
        )["records"]

        # Then
        self.assertEqual(
            [record["identity"] for record in records],
            ["ranked:two", "ranked:one", "ranked:null"],
        )

    def test_ranked_search_rejects_id_only_cursor(self) -> None:
        # Given
        memory = _memory("alpha", InMemoryStore(), create="auto")
        for identity in ("ranked:first", "ranked:second"):
            memory.execute_payload(
                f"propose --operation create --identity {identity} --text ranked"
            )
        cursor = memory.search("ranked", limit=1)["next_cursor"]

        # When / Then
        with self.assertRaises(CommandError):
            memory.search("ranked", limit=1, cursor=cursor)

    def test_schema_sort_requires_explicit_namespace(self) -> None:
        # Given
        memory = _memory("alpha", InMemoryStore(), create="auto")
        memory.execute_payload(
            "create-inventory --type scoped --fields "
            '\'{"priority":{"kind":"integer","filterable":true}}\''
        )

        # When / Then
        with self.assertRaises(CommandError):
            memory.search("", record_type="scoped", sort="priority")

    def test_doctor_preserves_policy_shape_when_postgres_is_unavailable(self) -> None:
        # Given
        memory = DurableMemory(
            settings=Settings(
                store="postgres",
                profile="alpha",
                policy=ApprovalPolicy(create="auto"),
                database_url="postgresql://unavailable",
            )
        )

        # When
        with patch.object(memory, "store", side_effect=CommandError("offline")):
            result = memory.doctor()

        # Then
        self.assertEqual(result["policy"], ApprovalPolicy(create="auto").as_dict())

    def test_expired_candidate_record_is_hidden_without_data_deletion(self) -> None:
        store = InMemoryStore()
        memory = _memory("alpha", store, create="auto")
        submitted = memory.submit_candidate(
            MemoryCandidate(
                record_type="fact",
                identity_key="expired:fact",
                payload={"value": "retain me"},
                text="retain me",
                valid_to=datetime.now(timezone.utc) - timedelta(seconds=1),
                evidence=(
                    MemoryEvidence(
                        source_kind="test",
                        source_ref="expired-record",
                        observed_at=datetime.now(timezone.utc),
                        confidence=1,
                    ),
                ),
            )
        )
        record_id = submitted["record_id"]
        self.assertEqual(memory.search("retain me")["records"], [])
        self.assertEqual(memory.expire_records()["affected"], 1)
        self.assertEqual(store.get_record(record_id).status, "expired")
        self.assertEqual(store.get_record(record_id).payload["value"], "retain me")
        self.assertTrue(store._state.candidates)  # noqa: SLF001

    def test_expiration_is_bounded_and_requires_valid_limit(self) -> None:
        memory = _memory("alpha", InMemoryStore())
        with self.assertRaises(CommandError):
            memory.expire_records(0)
        self.assertEqual(memory.doctor()["deployment_preflight"]["applicable"], False)

    def test_export_retention_and_two_admin_hard_purge(self) -> None:
        store = InMemoryStore()
        alpha = _memory("alpha", store, create="auto")
        created = alpha.execute_payload(
            "propose --operation create --identity private:purge --text retained"
        )
        exported = alpha.export()
        self.assertEqual(exported["profile"], "alpha")
        self.assertEqual(
            exported["namespaces"][0]["records"][0]["id"], created["record_id"]
        )

        alpha.set_retention_policy("profile:alpha", 60)
        beta = _memory("beta", store)
        alpha_profile = store.get_profile_by_slug("alpha")
        beta_profile = store.get_or_create_profile("beta")
        namespace = store.get_namespace("profile:alpha")
        store.grant(
            actor=alpha_profile,
            namespace=namespace,
            grantee=beta_profile,
            capability="admin",
        )
        request = alpha.request_hard_purge(
            "profile:alpha", created["record_id"], "privacy erasure request"
        )
        with self.assertRaises(CommandError):
            alpha.approve_hard_purge(request["id"])
        purged = beta.approve_hard_purge(request["id"])
        self.assertEqual(purged["status"], "purged")
        self.assertEqual(len(store._state.hard_purge_audit), 1)  # noqa: SLF001
        with self.assertRaises(CommandError):
            store.get_record(created["record_id"])

    def test_require_policy_hides_unapproved_facts_from_search(self) -> None:
        store = InMemoryStore()
        memory = _memory("alpha", store)
        payload = memory.execute_payload(
            "propose --operation create --type fact --identity user:name "
            "--text 'Name is Ada'"
        )

        self.assertEqual(payload["status"], "pending")
        self.assertIn("Waiting for approval", payload["message"])
        self.assertEqual(memory.search("Ada")["records"], [])

        approved = memory.execute_payload(f"approve --request-id {payload['id']}")
        self.assertEqual(approved["status"], "approved")
        self.assertIn("Saved", approved["message"])
        records = memory.search("Ada")["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["identity"], "user:name")

    def test_auto_policy_saves_immediately(self) -> None:
        store = InMemoryStore()
        memory = _memory("alpha", store, create="auto")
        payload = memory.execute_payload(
            "propose --operation create --identity user:city --text 'Lives in Lisbon'"
        )
        self.assertEqual(payload["status"], "approved")
        self.assertIn("Saved", payload["message"])
        self.assertEqual(len(memory.search("Lisbon")["records"]), 1)

    def test_private_namespaces_are_isolated(self) -> None:
        store = InMemoryStore()
        alpha = _memory("alpha", store, create="auto")
        beta = _memory("beta", store)
        alpha.execute_payload(
            "propose --operation create --identity secret --text 'alpha only'"
        )
        self.assertEqual(beta.search("alpha only")["records"], [])
        self.assertIn("No memory records matched", beta.execute("search --query alpha"))

    def test_shared_namespace_requires_explicit_grant(self) -> None:
        store = InMemoryStore()
        alpha = _memory("alpha", store, create="auto")
        beta = _memory("beta", store)
        alpha.execute_payload("create-namespace --slug household --kind shared")
        alpha.execute_payload(
            "propose --operation create --namespace household "
            "--identity movie:dune --text 'Dune'"
        )
        self.assertEqual(beta.search("Dune")["records"], [])
        alpha.execute_payload(
            "grant --namespace household --profile beta --capability read"
        )
        self.assertEqual(len(beta.search("Dune")["records"]), 1)

    def test_reserved_private_slug_cannot_be_taken_over(self) -> None:
        store = InMemoryStore()
        alpha = _memory("alpha", store)
        with self.assertRaises(CommandError):
            alpha.execute_payload("create-namespace --slug profile:beta --kind shared")
        beta_profile = store.get_or_create_profile("beta")
        store._state.namespaces_by_slug["profile:alpha"] = "taken"  # noqa: SLF001
        store._state.namespaces["taken"] = Namespace(  # noqa: SLF001
            id="taken",
            slug="profile:alpha",
            kind="private",
            owner_profile_id=beta_profile.id,
        )
        with self.assertRaises(CommandError):
            alpha.execute_payload("namespaces")

    def test_approve_is_idempotent_and_stale_update_is_superseded(self) -> None:
        store = InMemoryStore()
        memory = _memory("alpha", store, create="auto")
        created = memory.execute_payload(
            "propose --operation create --identity book:neuromancer "
            "--text 'Neuromancer'"
        )
        record_id = memory.search("Neuromancer")["records"][0]["id"]
        first = memory.execute_payload(
            "propose --operation update --record-id "
            f"{record_id} --text 'Neuromancer by Gibson' --expected-revision 1"
        )
        second = memory.execute_payload(
            "propose --operation update --record-id "
            f"{record_id} --text 'Neuromancer, 1984' --expected-revision 1"
        )
        memory.execute_payload(f"approve --request-id {first['id']}")
        again = memory.execute_payload(f"approve --request-id {first['id']}")
        stale = memory.execute_payload(f"approve --request-id {second['id']}")
        self.assertEqual(again["status"], "approved")
        self.assertEqual(stale["status"], "superseded")
        self.assertIn("out of date", stale["message"])
        self.assertIn("Gibson", memory.search("Neuromancer")["records"][0]["text"])
        record = memory.search("Neuromancer")["records"][0]
        self.assertEqual(record["payload"]["identity"], "book:neuromancer")
        self.assertIsNotNone(created["id"])

    def test_russian_dialogues(self) -> None:
        store = InMemoryStore()
        memory = _memory("alpha", store)
        with patch("hermes_durable_memory.i18n._language", return_value="ru"):
            payload = memory.execute_payload(
                "propose --operation create --identity user:name --text 'Имя Ада'"
            )
            self.assertIn("Ждёт подтверждения", payload["message"])
            pending = memory.execute("pending")
            self.assertIn("Подтвердить", pending)
            approved = memory.execute(f"approve --request-id {payload['id']}")
            self.assertIn("Сохранено", approved)
            self.assertIn("Найдено записей", memory.execute("search --query Ада"))

    def test_postgres_store_fails_closed(self) -> None:
        memory = DurableMemory(
            settings=Settings(
                store="postgres",
                profile="alpha",
                policy=ApprovalPolicy(),
            )
        )
        with self.assertRaises(CommandError):
            memory.execute("search --query test")

    def test_dynamic_inventory_validates_payload_and_derives_search_text(self) -> None:
        memory = _memory("alpha", InMemoryStore(), create="auto")
        definition = memory.execute_payload(
            "create-inventory --type person --fields "
            '\'{"name": {"kind": "string", "required": true, "searchable": true}, '
            '"age": {"kind": "integer", "filterable": true}}\''
        )
        self.assertEqual(definition["status"], "approved")
        self.assertEqual(
            memory.execute_payload("list-inventories")["inventories"][0]["type"],
            "person",
        )
        created = memory.execute_payload(
            "propose --operation create --type person --identity person:ada "
            '--payload \'{"name":"Ada","age":37}\''
        )
        self.assertEqual(created["status"], "approved")
        record = memory.search("Ada", record_type="person")["records"][0]
        self.assertEqual(record["text"], "Ada")
        self.assertEqual(record["payload"]["age"], 37)

    def test_dynamic_inventory_rejects_invalid_and_filters_records(self) -> None:
        memory = _memory("alpha", InMemoryStore(), create="auto")
        memory.execute_payload(
            "create-inventory --type task --fields "
            '\'{"title": {"kind": "string", "searchable": true}, '
            '"priority": {"kind": "integer", "filterable": true}}\''
        )
        with self.assertRaises(CommandError):
            memory.execute_payload(
                "propose --operation create --type task --identity task:bad "
                '--payload \'{"title":"Bad","priority":"high"}\''
            )
        for identity, priority in (("task:one", 1), ("task:two", 3)):
            memory.execute_payload(
                f"propose --operation create --type task --identity {identity} "
                f'--payload \'{{"title":"Task","priority":{priority}}}\''
            )
        result = memory.search(
            "Task",
            namespace_slug="profile:alpha",
            record_type="task",
            filters={"priority": {"gte": 2}},
        )
        self.assertEqual([item["identity"] for item in result["records"]], ["task:two"])

    def test_enum_inventory_field_requires_a_declared_value(self) -> None:
        memory = _memory("alpha", InMemoryStore(), create="auto")
        memory.execute_payload(
            "create-inventory --type recommendation --fields "
            '\'{"status":{"kind":"enum","values":["active","dismissed"]}}\''
        )
        with self.assertRaises(CommandError):
            memory.execute_payload(
                "propose --operation create --type recommendation "
                '--identity recommendation:one --payload \'{"status":"unknown"}\''
            )

    def test_filters_are_applied_before_the_in_memory_result_limit(self) -> None:
        memory = _memory("alpha", InMemoryStore(), create="auto")
        memory.execute_payload(
            "create-inventory --type task --fields "
            '\'{"title":{"kind":"string","searchable":true},'
            '"priority":{"kind":"integer","filterable":true}}\''
        )
        for index in range(60):
            memory.execute_payload(
                "propose --operation create --type task "
                f"--identity task:{index:03} "
                f'--payload \'{{"title":"Task","priority":{index}}}\''
            )

        result = memory.search(
            "Task",
            namespace_slug="profile:alpha",
            record_type="task",
            filters={"priority": {"gte": 59}},
            limit=1,
        )

        self.assertEqual([item["identity"] for item in result["records"]], ["task:059"])

    def test_update_merge_patch_preserves_fields_and_replace_is_explicit(self) -> None:
        memory = _memory("alpha", InMemoryStore(), create="auto", update="auto")
        memory.execute_payload(
            "create-inventory --type person --fields "
            '\'{"name":{"kind":"string","required":true},"age":{"kind":"integer"}}\''
        )
        memory.execute_payload(
            "propose --operation create --type person --identity person:ada "
            '--payload \'{"name":"Ada","age":37}\''
        )
        record = memory.search("person:ada")["records"][0]
        patched = memory.execute_payload(
            f"propose --operation update --record-id {record['id']} "
            "--payload '{\"age\":38}'"
        )
        updated = memory.search("person:ada")["records"][0]
        self.assertEqual(patched["update_mode"], "patch")
        self.assertEqual(updated["payload"]["name"], "Ada")
        self.assertEqual(updated["payload"]["age"], 38)
        with self.assertRaises(CommandError):
            memory.execute_payload(
                f"propose --operation update --record-id {record['id']} --replace true "
                "--payload '{\"age\":39}'"
            )

    def test_inventory_registry_creates_no_canonical_definition_record(self) -> None:
        store = InMemoryStore()
        memory = _memory("alpha", store, create="auto")
        memory.execute_payload(
            'create-inventory --type person --fields \'{"name":{"kind":"string"}}\''
        )
        self.assertEqual(memory.search("person")["records"], [])
        self.assertFalse(
            any(
                record.record_type == "__inventory_definition__"
                for record in store._state.records.values()  # noqa: SLF001
            )
        )
        definition = store._state.inventory_definitions  # noqa: SLF001
        self.assertEqual(len(definition), 1)
        with self.assertRaises(CommandError):
            store.propose(
                actor=store.get_or_create_profile("alpha"),
                namespace=store.get_or_create_private_namespace(
                    store.get_or_create_profile("alpha")
                ),
                operation="create",
                record_type="__inventory_definition__",
                identity_key="person",
                search_text="",
                payload={},
                policy_action="require",
                ttl_seconds=60,
            )

    def test_search_is_bounded_and_returns_fts_metadata_when_embeddings_disabled(
        self,
    ) -> None:
        memory = _memory("alpha", InMemoryStore(), create="auto")
        for number in range(3):
            memory.execute_payload(
                "propose --operation create "
                f"--identity note:{number} --text 'shared text'"
            )

        result = memory.search("shared", limit=2)

        self.assertEqual(len(result["records"]), 2)
        self.assertEqual(len(result["results"]), 2)
        self.assertTrue(all(item["source"] == "fts" for item in result["results"]))
        with self.assertRaises(CommandError):
            memory.search("shared", limit=51)

    def test_embedding_validation_rejects_non_finite_or_empty_vectors(self) -> None:
        self.assertEqual(validate_embedding([1, 2.5]), [1.0, 2.5])
        with self.assertRaises(CommandError):
            validate_embedding([])
        with self.assertRaises(CommandError):
            validate_embedding([float("nan")])
        with self.assertRaises(CommandError):
            validate_embedding([0.0, 0])

    def test_structured_search_supports_stable_cursor_and_schema_sort(self) -> None:
        memory = _memory("alpha", InMemoryStore(), create="auto")
        memory.execute_payload(
            "create-inventory --type task --fields "
            '\'{"title":{"kind":"string","searchable":true},'
            '"priority":{"kind":"integer","filterable":true}}\''
        )
        for identity, priority in (("task:one", 1), ("task:two", 2), ("task:three", 3)):
            memory.execute_payload(
                f"propose --operation create --type task --identity {identity} "
                f'--payload \'{{"title":"Task","priority":{priority}}}\''
            )
        first = memory.search(
            "Task",
            namespace_slug="profile:alpha",
            record_type="task",
            sort="priority",
            limit=2,
        )
        self.assertEqual(
            [item["identity"] for item in first["records"]], ["task:one", "task:two"]
        )
        second = memory.search(
            "Task",
            namespace_slug="profile:alpha",
            record_type="task",
            cursor=first["next_cursor"],
            sort="priority",
            limit=2,
        )
        self.assertEqual(
            [item["identity"] for item in second["records"]], ["task:three"]
        )

    def test_embedding_ingestion_rejects_invalid_hash_before_connecting(self) -> None:
        with self.assertRaises(CommandError):
            PostgresStore("postgresql://unused").complete_embedding_job(
                record_id="record",
                revision=1,
                content_hash="not-a-sha256",
                model_identifier="model",
                vector=[1.0],
            )

    def test_semantic_assessment_is_unsupported_in_memory_without_writes(self) -> None:
        store = InMemoryStore()
        memory = _memory("alpha", store, create="auto")
        created = memory.execute_payload(
            "propose --operation create --identity note:one --text unchanged"
        )
        request_count = len(store._state.requests)  # noqa: SLF001

        result = memory.assess_candidate_semantics()

        self.assertEqual(result, {"status": "unsupported", "assessed": 0, "failed": 0})
        self.assertEqual(len(store._state.requests), request_count)  # noqa: SLF001
        self.assertEqual(store.get_record(created["record_id"]).revision, 1)

    def test_embedding_requeue_apis_are_bounded_and_noop_in_memory(self) -> None:
        memory = _memory("alpha", InMemoryStore())

        self.assertEqual(
            memory.requeue_embedding_jobs(), {"status": "unsupported", "requeued": 0}
        )
        self.assertEqual(
            memory.requeue_candidate_embedding_jobs(),
            {"status": "unsupported", "requeued": 0},
        )
        for method in (
            memory.assess_candidate_semantics,
            memory.requeue_embedding_jobs,
            memory.requeue_candidate_embedding_jobs,
        ):
            with self.assertRaises(CommandError):
                method(51)

    def test_unavailable_candidate_provider_never_writes_canonical_records(
        self,
    ) -> None:
        class CandidateWorkerStore(PostgresStore):
            def __init__(self) -> None:
                super().__init__("postgresql://unused")
                self.failed: list[str] = []

            def get_or_create_profile(self, slug: str) -> Profile:
                return Profile(id="profile", slug=slug)

            def get_or_create_private_namespace(self, profile: Profile) -> Namespace:
                return Namespace(
                    id="namespace",
                    slug=f"profile:{profile.slug}",
                    kind="private",
                    owner_profile_id=profile.id,
                )

            def pending_candidate_embedding_jobs(self, **_kwargs):
                return [
                    {
                        "candidate_id": "candidate",
                        "text": "candidate text",
                        "claim_token": "claim",
                    }
                ]

            def fail_candidate_embedding_job(
                self, *, candidate_id: str, error: str, claim_token: str
            ) -> None:
                self.failed.append(candidate_id)

            def complete_candidate_embedding_job(self, **_kwargs) -> None:
                self.fail("unavailable providers must not complete assessments")

            def assess_candidate_semantics(self, **_kwargs) -> None:
                self.fail("unavailable providers must not assess candidates")

        store = CandidateWorkerStore()
        memory = DurableMemory(
            settings=Settings(
                store="postgres",
                profile="alpha",
                policy=ApprovalPolicy(),
                database_url="postgresql://unused",
                embedding_provider="ollama",
                ollama_base_url="http://localhost:11434",
                ollama_model="test",
            ),
            store=store,
        )
        with patch(
            "hermes_durable_memory.service.OllamaEmbeddingClient.embed",
            return_value=None,
        ):
            result = memory.assess_candidate_semantics()

        self.assertEqual(result, {"status": "ok", "assessed": 0, "failed": 1})
        self.assertEqual(store.failed, ["candidate"])

    def test_propose_grant_does_not_grant_read(self) -> None:
        store = InMemoryStore()
        alpha = _memory("alpha", store, create="auto")
        beta = _memory("beta", store)
        store.get_or_create_profile("beta")
        alpha.execute_payload("create-namespace --slug shared --kind shared")
        alpha.execute_payload(
            "grant --namespace shared --profile beta --capability propose"
        )
        proposed = beta.execute_payload(
            "propose --operation create --namespace shared "
            "--identity note:one --text secret"
        )
        self.assertEqual(proposed["status"], "pending")
        self.assertEqual(
            [item["slug"] for item in beta.execute_payload("namespaces")["namespaces"]],
            ["profile:beta", "shared"],
        )
        with self.assertRaises(CommandError):
            beta.execute_payload("search --namespace shared --query secret")

    def test_candidate_submission_is_approval_gated_and_keeps_evidence_separate(
        self,
    ) -> None:
        store = InMemoryStore()
        memory = _memory("alpha", store)
        candidate = MemoryCandidate(
            record_type="fact",
            identity_key="user:city",
            payload={"city": "Lisbon"},
            text="Lives in Lisbon",
            evidence=(
                MemoryEvidence(
                    source_kind="skill",
                    source_ref="run:123",
                    observed_at=datetime.now(timezone.utc),
                    confidence=0.9,
                    extractor_identity="profile-extractor",
                    extractor_version="1",
                ),
            ),
        )

        submitted = memory.submit_candidate(candidate)

        self.assertEqual(submitted["status"], "pending")
        self.assertEqual(memory.search("Lisbon")["records"], [])
        profile = store.get_or_create_profile("alpha")
        self.assertEqual(
            store.get_candidate(profile, submitted["candidate_id"]), candidate
        )
        memory.decide(submitted["id"], "approve")
        record = memory.search("Lisbon")["records"][0]
        self.assertNotIn("evidence", record["payload"])
        self.assertNotIn("source_ref", record["payload"])

    def test_candidate_rejects_invalid_evidence_confidence(self) -> None:
        with self.assertRaises(ValueError):
            MemoryEvidence(
                source_kind="skill",
                source_ref="run:123",
                observed_at=datetime.now(timezone.utc),
                confidence=1.1,
            )

    def test_propose_only_grant_can_submit_candidate_but_cannot_search(self) -> None:
        store = InMemoryStore()
        alpha = _memory("alpha", store, create="auto")
        beta = _memory("beta", store)
        store.get_or_create_profile("beta")
        alpha.execute_payload("create-namespace --slug shared --kind shared")
        alpha.execute_payload(
            "grant --namespace shared --profile beta --capability propose"
        )

        submitted = beta.submit_candidate(
            MemoryCandidate(
                namespace="shared",
                record_type="fact",
                identity_key="note:one",
                payload={},
                text="secret",
                evidence=(
                    MemoryEvidence(
                        source_kind="skill",
                        source_ref="run:456",
                        observed_at=datetime.now(timezone.utc),
                        confidence=1,
                    ),
                ),
            )
        )

        self.assertEqual(submitted["status"], "pending")
        with self.assertRaises(CommandError):
            beta.search("secret", namespace_slug="shared")

    @staticmethod
    def _candidate(
        *,
        identity: str,
        payload: dict[str, object],
        text: str,
        namespace: str | None = None,
    ) -> MemoryCandidate:
        return MemoryCandidate(
            namespace=namespace,
            record_type="fact",
            identity_key=identity,
            payload=payload,
            text=text,
            evidence=(
                MemoryEvidence(
                    source_kind="skill",
                    source_ref="run:conflict",
                    observed_at=datetime.now(timezone.utc),
                    confidence=1,
                ),
            ),
        )

    def test_exact_duplicate_candidate_has_no_create_request(self) -> None:
        store = InMemoryStore()
        memory = _memory("alpha", store, create="auto")
        created = memory.execute_payload(
            "propose --operation create --identity user:city "
            "--payload '{\"city\":\"Lisbon\"}' --text 'Lives in Lisbon'"
        )
        request_count = len(store._state.requests)

        submitted = memory.submit_candidate(
            self._candidate(
                identity="user:city",
                payload={"city": "Lisbon"},
                text="Lives in Lisbon",
            )
        )

        self.assertEqual(submitted["assessment"], "duplicate")
        self.assertEqual(submitted["matched_record_id"], created["record_id"])
        self.assertNotIn("id", submitted)
        self.assertEqual(len(store._state.requests), request_count)

    def test_new_candidate_honors_create_auto_policy(self) -> None:
        memory = _memory("alpha", InMemoryStore(), create="auto")

        submitted = memory.submit_candidate(
            self._candidate(
                identity="user:city",
                payload={"city": "Lisbon"},
                text="Lives in Lisbon",
            )
        )

        self.assertEqual(submitted["assessment"], "new")
        self.assertEqual(submitted["status"], "approved")
        self.assertEqual(memory.search("Lisbon")["records"][0]["identity"], "user:city")

    def test_conflicting_candidate_has_no_create_request(self) -> None:
        store = InMemoryStore()
        memory = _memory("alpha", store, create="auto")
        created = memory.execute_payload(
            "propose --operation create --identity user:city "
            "--payload '{\"city\":\"Lisbon\"}' --text 'Lives in Lisbon'"
        )
        request_count = len(store._state.requests)

        submitted = memory.submit_candidate(
            self._candidate(
                identity="user:city",
                payload={"city": "Porto"},
                text="Lives in Porto",
            )
        )

        self.assertEqual(submitted["assessment"], "conflict")
        self.assertEqual(submitted["matched_record_id"], created["record_id"])
        self.assertNotIn("id", submitted)
        self.assertEqual(len(store._state.requests), request_count)

    def test_consolidation_proposes_update_and_preserves_unrelated_fields(self) -> None:
        store = InMemoryStore()
        memory = _memory("alpha", store, create="auto", update="require")
        created = memory.execute_payload(
            "propose --operation create --identity user:city "
            '--payload \'{"city":"Lisbon","country":"Portugal"}\' '
            "--text 'Lives in Lisbon'"
        )
        submitted = memory.submit_candidate(
            self._candidate(
                identity="user:city",
                payload={"city": "Porto"},
                text="Lives in Porto",
            )
        )

        consolidated = memory.consolidate_candidate(submitted["candidate_id"])

        self.assertEqual(consolidated["status"], "pending")
        self.assertEqual(consolidated["operation"], "update")
        self.assertEqual(consolidated["record_id"], created["record_id"])
        self.assertEqual(consolidated["payload"]["city"], "Porto")
        self.assertEqual(consolidated["payload"]["country"], "Portugal")

    def test_approve_only_grant_can_consolidate_candidate(self) -> None:
        store = InMemoryStore()
        alpha = _memory("alpha", store, create="auto", update="require")
        beta = _memory("beta", store, update="require")
        store.get_or_create_profile("beta")
        alpha.execute_payload("create-namespace --slug shared --kind shared")
        alpha.execute_payload(
            "grant --namespace shared --profile beta --capability approve"
        )
        alpha.execute_payload(
            "propose --operation create --namespace shared --identity user:city "
            '--payload \'{"city":"Lisbon","country":"Portugal"}\' '
            "--text 'Lives in Lisbon'"
        )
        submitted = alpha.submit_candidate(
            self._candidate(
                namespace="shared",
                identity="user:city",
                payload={"city": "Porto", "metadata": {"source": "review"}},
                text="Lives in Porto",
            )
        )

        consolidated = beta.consolidate_candidate(submitted["candidate_id"])

        self.assertEqual(consolidated["status"], "pending")
        self.assertEqual(
            consolidated["requested_by_profile_id"],
            store.get_profile_by_slug("beta").id,
        )
        self.assertEqual(consolidated["payload"]["country"], "Portugal")
        self.assertEqual(consolidated["payload"]["metadata"], {"source": "review"})
        self.assertEqual(
            beta.consolidate_candidate(submitted["candidate_id"])["id"],
            consolidated["id"],
        )

    def test_propose_only_assessment_does_not_expose_record_details(self) -> None:
        store = InMemoryStore()
        alpha = _memory("alpha", store, create="auto")
        beta = _memory("beta", store)
        store.get_or_create_profile("beta")
        alpha.execute_payload("create-namespace --slug shared --kind shared")
        alpha.execute_payload(
            "grant --namespace shared --profile beta --capability propose"
        )
        alpha.execute_payload(
            "propose --operation create --namespace shared --identity user:city "
            "--payload '{\"city\":\"Lisbon\"}' --text 'private detail'"
        )

        submitted = beta.submit_candidate(
            self._candidate(
                namespace="shared",
                identity="user:city",
                payload={"city": "Porto"},
                text="other private detail",
            )
        )

        self.assertEqual(submitted["assessment"], "conflict")
        self.assertIn("matched_record_id", submitted)
        self.assertNotIn("payload", submitted)
        self.assertNotIn("text", submitted)
        with self.assertRaises(CommandError):
            beta.consolidate_candidate(submitted["candidate_id"])

    def test_inventory_fields_reject_truthy_flags_and_reserved_names(self) -> None:
        memory = _memory("alpha", InMemoryStore())
        for fields in (
            '{"name":{"kind":"string","searchable":"true"}}',
            '{"identity":{"kind":"string"}}',
            '{"__internal":{"kind":"string"}}',
        ):
            with self.assertRaises(CommandError):
                memory.execute_payload(
                    f"create-inventory --type person --fields '{fields}'"
                )

    def test_filters_reject_unknown_operators_and_support_in(self) -> None:
        memory = _memory("alpha", InMemoryStore(), create="auto")
        memory.execute_payload(
            "create-inventory --type person --fields "
            '\'{"name":{"kind":"string","filterable":true}}\''
        )
        memory.execute_payload(
            "propose --operation create --type person --identity person:ada "
            '--payload \'{"name":"Ada"}\''
        )
        result = memory.search(
            "Ada",
            namespace_slug="profile:alpha",
            record_type="person",
            filters={"name": {"in": ["Ada"]}},
        )
        self.assertEqual(result["records"][0]["identity"], "person:ada")
        with self.assertRaises(CommandError):
            memory.search(
                "Ada",
                namespace_slug="profile:alpha",
                record_type="person",
                filters={"name": {"regex": ".*"}},
            )


if __name__ == "__main__":
    unittest.main()
