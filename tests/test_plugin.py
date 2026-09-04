from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hermes_durable_memory.config import Settings
from hermes_durable_memory.extraction import TurnContext
from hermes_durable_memory.general_plugin import register
from hermes_durable_memory.memory_entrypoint import create_provider
from hermes_durable_memory.models import MemoryCandidate, MemoryEvidence
from hermes_durable_memory.policies import ApprovalPolicy
from hermes_durable_memory.provider import DurableMemoryProvider
from hermes_durable_memory.service import DurableMemory
from hermes_durable_memory.store import InMemoryStore


class FakeHumanDecisions:
    def __init__(self, result=None) -> None:
        self.result = result or {"ok": False, "error": "capability_not_granted"}
        self.requests = []

    async def request(self, **kwargs):
        self.requests.append(kwargs)
        return self.result


class FakeContext:
    def __init__(self) -> None:
        self.commands = []
        self.cli_commands = []
        self.human_decisions = FakeHumanDecisions()

    def register_command(self, *args, **kwargs) -> None:
        self.commands.append((args, kwargs))

    def register_cli_command(self, *args, **kwargs) -> None:
        self.cli_commands.append((args, kwargs))


class PluginRegistrationTests(unittest.TestCase):
    def test_general_plugin_registers_commands_and_cli(self) -> None:
        context = FakeContext()

        register(context)

        self.assertEqual(context.commands[0][0][0], "durable-memory")
        self.assertEqual(context.cli_commands[0][0][0], "durable-memory")

    def test_provider_factory_and_tool_do_not_expose_admin_actions(self) -> None:
        provider = create_provider()
        schema = provider.get_tool_schemas()[0]

        self.assertEqual(provider.name, "durable-memory")
        actions = schema["parameters"]["properties"]["action"]["enum"]
        self.assertNotIn("approve", actions)
        self.assertNotIn("grant", actions)
        self.assertNotIn("migrate", actions)

    def test_provider_factory_supports_legacy_collector_loading(self) -> None:
        class Collector:
            provider = None

            def register_memory_provider(self, provider) -> None:
                self.provider = provider

        collector = Collector()
        provider = create_provider(collector)
        self.assertIs(collector.provider, provider)

    def test_provider_tool_rejects_actions_outside_runtime_allowlist(self) -> None:
        provider = DurableMemoryProvider(
            DurableMemory(
                settings=Settings(
                    store="memory", profile="alpha", policy=ApprovalPolicy()
                ),
                store=InMemoryStore(),
            )
        )

        for action in ("approve", "grant", "migrate", "pending", "unknown"):
            payload = json.loads(
                provider.handle_tool_call("durable_memory", {"action": action})
            )
            self.assertFalse(payload["ok"])

    def test_tool_returns_localized_message_and_payload(self) -> None:
        memory = DurableMemory(
            settings=Settings(
                store="memory",
                profile="alpha",
                policy=ApprovalPolicy(create="auto"),
            ),
            store=InMemoryStore(),
        )
        raw = DurableMemoryProvider(memory).handle_tool_call(
            "durable_memory",
            {
                "action": "propose",
                "operation": "create",
                "identity": "user:name",
                "text": "Name is Ada",
            },
        )
        payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "approved")
        self.assertIn("Saved", payload["message"])

    def test_tool_accepts_structured_payload_and_inventory_actions(self) -> None:
        memory = DurableMemory(
            settings=Settings(
                store="memory", profile="alpha", policy=ApprovalPolicy(create="auto")
            ),
            store=InMemoryStore(),
        )
        handler = DurableMemoryProvider(memory).handle_tool_call
        created = json.loads(
            handler(
                "durable_memory",
                {
                    "action": "create-inventory",
                    "type": "person",
                    "fields": {"name": {"kind": "string", "searchable": True}},
                },
            )
        )
        self.assertTrue(created["ok"])
        proposed = json.loads(
            handler(
                "durable_memory",
                {
                    "action": "propose",
                    "operation": "create",
                    "type": "person",
                    "identity": "person:ada",
                    "payload": {"name": "Ada"},
                },
            )
        )
        self.assertTrue(proposed["ok"])
        self.assertEqual(proposed["status"], "approved")

    def test_tool_does_not_request_inline_decision(self) -> None:
        memory = DurableMemory(
            settings=Settings(store="memory", profile="alpha", policy=ApprovalPolicy()),
            store=InMemoryStore(),
        )
        decisions = FakeHumanDecisions({"ok": True, "decision": "approve"})
        raw = DurableMemoryProvider(memory).handle_tool_call(
            "durable_memory",
            {
                "action": "propose",
                "operation": "create",
                "identity": "user:name",
                "text": "Name is Ada",
            },
        )
        payload = json.loads(raw)

        self.assertEqual(payload["status"], "pending")
        self.assertEqual(decisions.requests, [])

    def test_slash_command_applies_human_decision(self) -> None:
        memory = DurableMemory(
            settings=Settings(store="memory", profile="alpha", policy=ApprovalPolicy()),
            store=InMemoryStore(),
        )
        decisions = FakeHumanDecisions(
            {
                "ok": True,
                "decision": "approve",
                "request_id": "human-1",
                "actor_id": "42",
            }
        )
        from hermes_durable_memory.general_plugin import command_handler

        result = asyncio.run(
            command_handler(memory, decisions)(
                "propose --operation create --identity user:name --text 'Name is Ada'",
                session_key="telegram:session",
            )
        )

        self.assertIn("Saved", result)
        self.assertEqual(decisions.requests[0]["session_key"], "telegram:session")
        self.assertEqual(len(memory.search("Ada")["records"]), 1)

    def test_provider_prefetch_uses_active_records(self) -> None:
        memory = DurableMemory(
            settings=Settings(
                store="memory",
                profile="alpha",
                policy=ApprovalPolicy(create="auto"),
            ),
            store=InMemoryStore(),
        )
        memory.execute(
            "propose --operation create --identity user:name --text 'Name is Ada'"
        )
        provider = DurableMemoryProvider(memory)
        self.assertEqual(provider.prefetch("Ada"), "")
        provider.queue_prefetch("Ada")
        self.assertIn("Name is Ada", provider.prefetch("Ada"))
        provider.on_session_switch("session:next")
        self.assertEqual(provider.prefetch("Ada"), "")
        self.assertEqual(provider.get_tool_schemas()[0]["name"], "durable_memory")
        provider.queue_prefetch("Ada")
        provider.shutdown()
        self.assertEqual(provider.prefetch("Ada"), "")

    def test_provider_tool_hides_unexpected_backend_errors(self) -> None:
        class FailingMemory(DurableMemory):
            def execute_payload(self, _raw_args):
                raise RuntimeError("database implementation detail")

        payload = json.loads(
            DurableMemoryProvider(FailingMemory()).handle_tool_call(
                "durable_memory", {"action": "doctor"}
            )
        )
        self.assertEqual(
            payload,
            {"ok": False, "error": "Durable Memory is temporarily unavailable."},
        )

    def test_provider_sync_without_explicit_candidates_or_extractors_is_a_noop(
        self,
    ) -> None:
        store = InMemoryStore()
        provider = DurableMemoryProvider(
            DurableMemory(
                settings=Settings(
                    store="memory", profile="alpha", policy=ApprovalPolicy()
                ),
                store=store,
            )
        )

        self.assertIsNone(provider.sync_turn("user", "assistant", session_id="test"))
        self.assertEqual(store._state.requests, {})  # noqa: SLF001
        self.assertEqual(store._state.candidates, {})  # noqa: SLF001

    def test_provider_sync_extracts_candidates_without_service_or_store_access(
        self,
    ) -> None:
        store = InMemoryStore()
        received = []

        class Extractor:
            def extract(self, context: TurnContext):
                received.append(context)
                return [
                    MemoryCandidate(
                        record_type="fact",
                        identity_key="user:city",
                        payload={"city": "Lisbon"},
                        text="Lives in Lisbon",
                        evidence=(
                            MemoryEvidence(
                                source_kind="skill",
                                source_ref="turn:1",
                                observed_at=datetime.now(timezone.utc),
                                confidence=0.9,
                            ),
                        ),
                    )
                ]

        memory = DurableMemory(
            settings=Settings(store="memory", profile="alpha", policy=ApprovalPolicy()),
            store=store,
        )
        memory.register_extractor(Extractor())
        provider = DurableMemoryProvider(memory)
        self.assertIsNone(
            provider.sync_turn(
                "Lives in Lisbon",
                "Noted",
                session_id="session:1",
                messages=[{"role": "user", "content": "Lives in Lisbon"}],
            )
        )

        self.assertEqual(received[0].session_id, "session:1")
        self.assertFalse(hasattr(received[0], "store"))
        self.assertEqual(len(store._state.candidates), 1)  # noqa: SLF001

    def test_provider_sync_ignores_invalid_extractor_output_and_isolates_errors(
        self,
    ) -> None:
        store = InMemoryStore()

        class InvalidExtractor:
            def extract(self, _context: TurnContext):
                return [{"text": "missing candidate evidence"}]

        class FailingExtractor:
            def extract(self, _context: TurnContext):
                raise OSError("unavailable")

        provider = DurableMemoryProvider(
            DurableMemory(
                settings=Settings(
                    store="memory", profile="alpha", policy=ApprovalPolicy()
                ),
                store=store,
                extractors=(InvalidExtractor(), FailingExtractor()),
            )
        )

        self.assertIsNone(
            provider.sync_turn("user", "assistant", session_id="session:1")
        )
        self.assertEqual(store._state.requests, {})  # noqa: SLF001
        self.assertEqual(store._state.candidates, {})  # noqa: SLF001

    def test_provider_sync_submits_candidates_and_swallows_errors(
        self,
    ) -> None:
        class FailingMemory(DurableMemory):
            def submit_candidate(self, candidate):
                raise OSError("database unavailable")

        candidate = MemoryCandidate(
            record_type="fact",
            identity_key="user:city",
            payload={"city": "Lisbon"},
            evidence=(
                MemoryEvidence(
                    source_kind="skill",
                    source_ref="turn:1",
                    observed_at=datetime.now(timezone.utc),
                    confidence=0.9,
                ),
            ),
        )

        class Extractor:
            def extract(self, _context: TurnContext):
                return [candidate]

        provider = DurableMemoryProvider(
            FailingMemory(
                settings=Settings(
                    store="memory", profile="alpha", policy=ApprovalPolicy()
                ),
                store=InMemoryStore(),
                extractors=(Extractor(),),
            )
        )

        self.assertIsNone(
            provider.sync_turn("user", "assistant", session_id="session:1")
        )

    def test_turn_context_rejects_oversized_session_identifier(self) -> None:
        with self.assertRaises(ValueError):
            TurnContext(session_id="x" * 20_000)

    def test_directory_plugin_shim_imports(self) -> None:
        root = Path(__file__).parents[1]
        package_name = "hermes_plugins.durable_memory_test"
        parent = types.ModuleType("hermes_plugins")
        parent.__path__ = []
        sys.modules["hermes_plugins"] = parent
        try:
            spec = importlib.util.spec_from_file_location(
                package_name,
                root / "__init__.py",
                submodule_search_locations=[str(root)],
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[package_name] = module
            assert spec.loader is not None
            spec.loader.exec_module(module)
            self.assertTrue(callable(module.register))
        finally:
            sys.modules.pop(package_name, None)
            sys.modules.pop("hermes_plugins", None)


if __name__ == "__main__":
    unittest.main()
