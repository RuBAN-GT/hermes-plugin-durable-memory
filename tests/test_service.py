from __future__ import annotations

import unittest
from unittest.mock import patch

from hermes_durable_memory.config import Settings
from hermes_durable_memory.models import CommandError
from hermes_durable_memory.policies import ApprovalPolicy
from hermes_durable_memory.service import DurableMemory
from hermes_durable_memory.store import InMemoryStore


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
            "Task", record_type="task", filters={"priority": {"gte": 2}}
        )
        self.assertEqual([item["identity"] for item in result["records"]], ["task:two"])


if __name__ == "__main__":
    unittest.main()
