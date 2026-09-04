"""Validate an installed wheel, optionally against a Hermes source checkout."""

from __future__ import annotations

import argparse
import importlib.metadata
import sys
import sysconfig
from pathlib import Path

PLUGIN_ENTRY_POINT = "hermes_durable_memory.general_plugin:register"
PROVIDER_ENTRY_POINT = "hermes_durable_memory.memory_entrypoint:create_provider"
CAPABILITY_NAME = "durable-memory.gateway.human_decisions"


def _entry_points(group: str) -> list[importlib.metadata.EntryPoint]:
    return list(importlib.metadata.entry_points().select(group=group))


def _entry_point(group: str, name: str, value: str) -> importlib.metadata.EntryPoint:
    matches = [entry for entry in _entry_points(group) if entry.name == name]
    assert [(entry.name, entry.value) for entry in matches] == [(name, value)]
    return matches[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-root", type=Path)
    args = parser.parse_args()
    if args.hermes_root is not None:
        sys.path.insert(0, str(args.hermes_root.resolve()))

    plugin = _entry_point("hermes_agent.plugins", "durable-memory", PLUGIN_ENTRY_POINT)
    capability = _entry_point(
        "hermes_agent.plugin_capabilities", CAPABILITY_NAME, PLUGIN_ENTRY_POINT
    )
    provider = _entry_point(
        "hermes_agent.memory_providers", "durable-memory", PROVIDER_ENTRY_POINT
    )

    import hermes_durable_memory

    purelib = Path(sysconfig.get_paths()["purelib"]).resolve()
    assert purelib in Path(hermes_durable_memory.__file__).resolve().parents
    assert callable(plugin.load())
    assert capability.load() is plugin.load()
    loaded_provider = provider.load()()
    assert loaded_provider.name == "durable-memory"

    if args.hermes_root is None:
        return

    from agent.memory_provider import MemoryProvider
    from hermes_cli.plugins import discover_entrypoint_manifests
    from plugins.memory import list_memory_provider_names, load_memory_provider

    manifest = next(
        item
        for item in discover_entrypoint_manifests()
        if item.name == "durable-memory"
    )
    assert manifest.kind == "standalone"
    assert manifest.capabilities == ["gateway.human_decisions"]
    assert "durable-memory" in list_memory_provider_names()
    loaded_provider = load_memory_provider("durable-memory", register_skills=False)
    assert isinstance(loaded_provider, MemoryProvider)
    assert loaded_provider.name == "durable-memory"


if __name__ == "__main__":
    main()
