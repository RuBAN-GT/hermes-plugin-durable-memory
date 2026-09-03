from __future__ import annotations

from typing import Any

from .service import DurableMemory

try:
    from agent.memory_provider import MemoryProvider
except ImportError:

    class MemoryProvider:
        """Development fallback when Hermes is not installed."""


class DurableMemoryProvider(MemoryProvider):
    """Hermes MemoryProvider for cross-session durable recall."""

    def __init__(self, memory: DurableMemory) -> None:
        self._memory = memory

    @property
    def name(self) -> str:
        return "durable-memory"

    def is_available(self) -> bool:
        try:
            settings = self._memory.settings
            return settings.store == "memory" or bool(settings.database_url)
        except Exception:
            return False

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id
        self._hermes_home = kwargs.get("hermes_home")

    def prefetch(self, query: str, **_kwargs: Any) -> str:
        try:
            return self._memory.prefetch_text(query)
        except Exception:
            return ""

    def sync_turn(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """The plugin registers its global tool through PluginContext."""
        return []

    def on_session_end(self, *_args: Any, **_kwargs: Any) -> None:
        return None
