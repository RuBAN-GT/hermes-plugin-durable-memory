from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

from hermes_durable_memory.config import Settings
from hermes_durable_memory.plugin import register
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
        self.tools = []
        self.memory_providers = []
        self.human_decisions = FakeHumanDecisions()

    def register_command(self, *args, **kwargs) -> None:
        self.commands.append((args, kwargs))

    def register_cli_command(self, *args, **kwargs) -> None:
        self.cli_commands.append((args, kwargs))

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)

    def register_memory_provider(self, provider) -> None:
        self.memory_providers.append(provider)


class PluginRegistrationTests(unittest.TestCase):
    def test_registers_commands_tool_and_memory_provider(self) -> None:
        context = FakeContext()

        register(context)

        self.assertEqual(context.commands[0][0][0], "durable-memory")
        self.assertEqual(context.cli_commands[0][0][0], "durable-memory")
        self.assertEqual(context.tools[0]["name"], "durable_memory")
        self.assertTrue(context.tools[0]["is_async"])
        self.assertEqual(context.memory_providers[0].name, "durable-memory")

    def test_tool_does_not_expose_approval_or_administration(self) -> None:
        context = FakeContext()
        register(context)

        actions = context.tools[0]["schema"]["properties"]["action"]["enum"]
        self.assertNotIn("approve", actions)
        self.assertNotIn("grant", actions)
        self.assertNotIn("migrate", actions)

    def test_tool_returns_localized_message_and_payload(self) -> None:
        memory = DurableMemory(
            settings=Settings(
                store="memory",
                profile="alpha",
                policy=ApprovalPolicy(create="auto"),
            ),
            store=InMemoryStore(),
        )
        from hermes_durable_memory.plugin import _tool_handler

        raw = asyncio.run(
            _tool_handler(memory)(
                {
                    "action": "propose",
                    "operation": "create",
                    "identity": "user:name",
                    "text": "Name is Ada",
                }
            )
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
        from hermes_durable_memory.plugin import _tool_handler

        handler = _tool_handler(memory)
        created = json.loads(
            asyncio.run(
                handler(
                    {
                        "action": "create-inventory",
                        "type": "person",
                        "fields": {"name": {"kind": "string", "searchable": True}},
                    }
                )
            )
        )
        self.assertTrue(created["ok"])
        proposed = json.loads(
            asyncio.run(
                handler(
                    {
                        "action": "propose",
                        "operation": "create",
                        "type": "person",
                        "identity": "person:ada",
                        "payload": {"name": "Ada"},
                    }
                )
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
        from hermes_durable_memory.plugin import _tool_handler

        raw = asyncio.run(
            _tool_handler(memory)(
                {
                    "action": "propose",
                    "operation": "create",
                    "identity": "user:name",
                    "text": "Name is Ada",
                }
            )
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
        from hermes_durable_memory.plugin import command_handler

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
        self.assertIn("Name is Ada", provider.prefetch("Ada"))
        self.assertEqual(provider.get_tool_schemas(), [])

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
