from __future__ import annotations

import json
import shlex
from collections.abc import Iterable
from itertools import islice
from typing import Any

from .extraction import TurnContext
from .models import CommandError, MemoryCandidate
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
        self._session_id = ""
        self._prefetch_cache: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "durable-memory"

    def is_available(self) -> bool:
        try:
            settings = self._memory.settings
            return settings.store == "memory" or bool(settings.database_url)
        except Exception:
            return False

    def unavailable_reason(self) -> str | None:
        try:
            settings = self._memory.settings
        except Exception:
            return "Durable Memory configuration is unavailable."
        if settings.store == "postgres" and not settings.database_url:
            return "DURABLE_MEMORY_DATABASE_URL is required for PostgreSQL memory."
        return None if self.is_available() else "Durable Memory is not configured."

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id
        self._prefetch_cache.clear()
        self._hermes_home = kwargs.get("hermes_home")

    def prefetch(self, query: str, **_kwargs: Any) -> str:
        """Return cached recall only; never block the request path on I/O."""
        return self._prefetch_cache.get(query, "")

    def queue_prefetch(self, query: str, **_kwargs: Any) -> None:
        """Prepare bounded recall on Hermes' serialized prefetch worker."""
        try:
            self._prefetch_cache[query] = self._memory.prefetch_text(query)
        except Exception:
            self._prefetch_cache.pop(query, None)

    _MAX_CANDIDATES_PER_TURN = 32

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
        **_kwargs: Any,
    ) -> None:
        """Run registered extractors against Hermes' bounded turn contract."""
        if not self._memory.extractors or not session_id:
            return None
        turn_messages = tuple((messages or [])[-8:])
        if not turn_messages:
            turn_messages = (
                {"role": "user", "content": user_content},
                {"role": "assistant", "content": assistant_content},
            )
        try:
            context = TurnContext(
                session_id=session_id,
                messages=turn_messages,
                source={"provider": self.name},
            )
        except ValueError:
            return None
        candidates: list[MemoryCandidate] = []
        for extractor in self._memory.extractors:
            try:
                extracted = extractor.extract(context)
                candidates.extend(self._candidate_items(extracted))
            except Exception:
                continue
        for candidate in candidates:
            try:
                self._memory.submit_candidate(candidate)
            except Exception:
                continue
        return None

    @staticmethod
    def _candidate_items(value: Any) -> list[MemoryCandidate]:
        if isinstance(value, MemoryCandidate):
            return [value]
        if not isinstance(value, Iterable) or isinstance(value, (str, bytes, dict)):
            return []
        return [
            item
            for item in islice(value, DurableMemoryProvider._MAX_CANDIDATES_PER_TURN)
            if isinstance(item, MemoryCandidate)
        ]

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Expose only safe memory actions through Hermes' memory toolset."""
        return [
            {
                "name": "durable_memory",
                "description": (
                    "Search durable memory or propose create/update/delete. "
                    "Writes follow the profile approval policy."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "doctor",
                                "namespaces",
                                "create-inventory",
                                "list-inventories",
                                "search",
                                "propose",
                            ],
                        },
                        "query": {"type": "string"},
                        "namespace": {"type": "string"},
                        "operation": {
                            "type": "string",
                            "enum": ["create", "update", "delete"],
                        },
                        "type": {"type": "string"},
                        "identity": {"type": "string"},
                        "text": {"type": "string"},
                        "record-id": {"type": "string"},
                        "expected-revision": {"type": "integer"},
                        "replace": {"type": "boolean"},
                        "slug": {"type": "string"},
                        "kind": {"type": "string", "enum": ["private", "shared"]},
                        "payload": {"type": "object"},
                        "filter": {"type": "object"},
                        "filters": {"type": "object"},
                        "cursor": {"type": "string"},
                        "sort": {"type": "string"},
                        "descending": {"type": "boolean"},
                        "fields": {"type": "object"},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            }
        ]

    def handle_tool_call(
        self, tool_name: str, args: dict[str, Any], **_kwargs: Any
    ) -> str:
        if tool_name != "durable_memory" or not isinstance(args, dict):
            return json.dumps({"ok": False, "error": "Unknown memory tool."})
        action = args.get("action")
        allowed_actions = {
            "doctor",
            "namespaces",
            "create-inventory",
            "list-inventories",
            "search",
            "propose",
        }
        if action not in allowed_actions:
            return json.dumps({"ok": False, "error": "Unsupported memory action."})
        options: list[str] = []
        for key, value in args.items():
            if key == "action" or value is None or value == "":
                continue
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            options.extend([f"--{key}", str(value)])
        try:
            payload = self._memory.execute_payload(shlex.join([action, *options]))
            return json.dumps({"ok": True, **payload}, ensure_ascii=False)
        except (CommandError, OSError, ValueError) as error:
            return json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False)
        except Exception:
            return json.dumps(
                {"ok": False, "error": "Durable Memory is temporarily unavailable."}
            )

    def on_session_switch(self, session_id: str, **_kwargs: Any) -> None:
        self._session_id = session_id
        self._prefetch_cache.clear()

    def on_session_end(self, *_args: Any, **_kwargs: Any) -> None:
        self._session_id = ""
        self._prefetch_cache.clear()
        return None

    def shutdown(self) -> None:
        """Release process-local, session-scoped state without touching records."""
        self._session_id = ""
        self._prefetch_cache.clear()
