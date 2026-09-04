"""Read-only adapter for the Hermes Holographic SQLite fact store."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..models import MemoryCandidate, MemoryEvidence


class HolographicSQLiteSource:
    """Map Holographic ``facts`` rows to approval-gated candidates.

    The adapter never creates or modifies source tables. Its checkpoint is the
    monotonically increasing Holographic ``fact_id`` and can be stored by an
    operator after each returned report.
    """

    source_name = "holographic"

    def __init__(self, database_path: str | Path, *, namespace: str | None = None):
        self._path = Path(database_path).expanduser().resolve()
        self._namespace = namespace

    def page(
        self, checkpoint: str | None, limit: int
    ) -> tuple[list[MemoryCandidate], str | None]:
        if not self._path.is_file():
            raise ValueError("Holographic source database does not exist.")
        after = int(checkpoint or 0)
        if after < 0:
            raise ValueError("Holographic checkpoint must be non-negative.")
        uri = f"file:{self._path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            rows = connection.execute(
                "SELECT fact_id, content, category, tags, trust_score, created_at "
                "FROM facts WHERE fact_id > ? ORDER BY fact_id LIMIT ?",
                (after, limit),
            ).fetchall()
        finally:
            connection.close()
        candidates = [self._candidate(row) for row in rows]
        return candidates, str(rows[-1][0]) if rows else None

    def _candidate(self, row: tuple[object, ...]) -> MemoryCandidate:
        fact_id, content, category, tags, trust_score, created_at = row
        observed_at = self._observed_at(str(created_at or ""))
        confidence = float(trust_score) if trust_score is not None else 0.5
        confidence = min(1.0, max(0.0, confidence))
        source_ref = f"holographic:fact:{fact_id}"
        return MemoryCandidate(
            record_type="holographic_fact",
            identity_key=source_ref,
            payload={
                "identity": source_ref,
                "content": str(content),
                "category": str(category or "general"),
                "tags": str(tags or ""),
                "source_system": self.source_name,
                "source_id": str(fact_id),
            },
            text=str(content),
            namespace=self._namespace,
            evidence=(
                MemoryEvidence(
                    source_kind=self.source_name,
                    source_ref=source_ref,
                    observed_at=observed_at,
                    confidence=confidence,
                ),
            ),
        )

    @staticmethod
    def _observed_at(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
