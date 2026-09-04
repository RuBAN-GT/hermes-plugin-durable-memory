"""Narrow, transport-independent contract for optional memory extractors."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .models import MemoryCandidate

MAX_TURN_MESSAGES = 8
MAX_TURN_CONTEXT_BYTES = 16_384


@dataclass(frozen=True)
class TurnContext:
    """Explicit bounded input supplied to a candidate extractor for one turn.

    This object is transient. Durable memory persists only candidates accepted by
    ``DurableMemory.submit_candidate``; it never persists turn context itself.
    """

    session_id: str
    messages: tuple[dict[str, Any], ...] = ()
    context: dict[str, Any] = field(default_factory=dict)
    source: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("Turn context session_id is required.")
        if (
            not isinstance(self.messages, tuple)
            or len(self.messages) > MAX_TURN_MESSAGES
            or not all(isinstance(message, dict) for message in self.messages)
        ):
            raise ValueError(
                "Turn context messages must be a bounded tuple of objects."
            )
        if not isinstance(self.context, dict):
            raise ValueError("Turn context must be an object.")
        if not isinstance(self.source, dict) or not all(
            isinstance(key, str) and key and isinstance(value, str) and value
            for key, value in self.source.items()
        ):
            raise ValueError("Turn source metadata must contain non-empty strings.")
        try:
            encoded = json.dumps(
                {
                    "session_id": self.session_id,
                    "messages": self.messages,
                    "context": self.context,
                    "source": self.source,
                },
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
        except (TypeError, ValueError) as error:
            raise ValueError("Turn context must be JSON serializable.") from error
        if len(encoded) > MAX_TURN_CONTEXT_BYTES:
            raise ValueError("Turn context exceeds the maximum size.")


class MemoryCandidateExtractor(Protocol):
    """Extracts candidates from transient turn context without storage access."""

    def extract(self, context: TurnContext) -> Iterable[MemoryCandidate]: ...
