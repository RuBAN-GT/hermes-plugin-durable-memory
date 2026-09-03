"""Optional Ollama embedding adapter using only the Python standard library."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class OllamaConfig:
    """Configuration supplied by the process environment, never by dotenv."""

    base_url: str | None = None
    model: str | None = None
    timeout_seconds: float = 10.0

    @property
    def enabled(self) -> bool:
        parsed = urlparse(self.base_url or "")
        return bool(
            self.model
            and parsed.scheme in {"http", "https"}
            and parsed.netloc
            and self.timeout_seconds > 0
        )


class OllamaEmbeddingClient:
    """Fail-closed client for Ollama's documented ``POST /api/embed`` API."""

    def __init__(self, config: OllamaConfig) -> None:
        self.config = config

    @classmethod
    def from_settings(cls, settings: Any) -> OllamaEmbeddingClient:
        """Build from injected settings; an unrelated provider stays disabled."""
        provider = getattr(settings, "embedding_provider", None)
        return cls(
            OllamaConfig(
                base_url=getattr(settings, "ollama_base_url", None)
                if provider == "ollama"
                else None,
                model=getattr(settings, "ollama_model", None)
                if provider == "ollama"
                else None,
                timeout_seconds=getattr(settings, "ollama_timeout_seconds", 10.0),
            )
        )

    def embed(self, text: str) -> list[float] | None:
        """Return the first embedding, or ``None`` if disabled or unavailable."""
        if not self.config.enabled or not isinstance(text, str) or not text:
            return None
        endpoint = self.config.base_url.rstrip("/") + "/api/embed"
        body = json.dumps({"model": self.config.model, "input": text}).encode()
        request = Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                payload: Any = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            return None
        embeddings = payload.get("embeddings") if isinstance(payload, dict) else None
        if not isinstance(embeddings, list) or not embeddings:
            return None
        vector = embeddings[0]
        if not isinstance(vector, list) or not all(
            isinstance(value, (int, float)) for value in vector
        ):
            return None
        return [float(value) for value in vector]
