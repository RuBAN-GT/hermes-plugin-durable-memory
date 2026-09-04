"""Transport-independent, bounded candidate import orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .models import MemoryCandidate


class CandidateSource(Protocol):
    """Read-only paged source with stable source identities."""

    def page(
        self, checkpoint: str | None, limit: int
    ) -> tuple[list[MemoryCandidate], str | None]: ...


class CandidateSubmitter(Protocol):
    def submit_candidate(self, candidate: MemoryCandidate) -> dict[str, object]: ...


class ImportCheckpointStore(Protocol):
    """Durable checkpoint storage owned by the target memory store."""

    def load_import_checkpoint(
        self, *, source: str, scope: str
    ) -> dict[str, Any] | None: ...

    def save_import_checkpoint(
        self, *, source: str, scope: str, checkpoint: str | None, report: dict[str, Any]
    ) -> None: ...


@dataclass(frozen=True)
class ImportReport:
    source: str
    dry_run: bool
    seen: int
    proposed: int
    duplicates: int
    conflicts: int
    rejected: int
    checkpoint: str | None
    importer_version: str = "1"

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "dry_run": self.dry_run,
            "seen": self.seen,
            "proposed": self.proposed,
            "duplicates": self.duplicates,
            "conflicts": self.conflicts,
            "rejected": self.rejected,
            "checkpoint": self.checkpoint,
            "importer_version": self.importer_version,
            "explained": self.seen
            == self.proposed + self.duplicates + self.conflicts + self.rejected,
        }


def import_candidates(
    submitter: CandidateSubmitter,
    source: CandidateSource,
    *,
    source_name: str,
    checkpoint: str | None = None,
    batch_size: int = 100,
    dry_run: bool = False,
    checkpoint_store: ImportCheckpointStore | None = None,
    scope: str = "default",
) -> ImportReport:
    """Import bounded pages and return a resumable checkpoint.

    A checkpoint advances only after every candidate in its page has been
    submitted successfully. The source remains read-only; canonical writes go
    exclusively through ``submit_candidate`` and therefore through approval.
    """
    if (
        not isinstance(batch_size, int)
        or isinstance(batch_size, bool)
        or not 1 <= batch_size <= 500
    ):
        raise ValueError("Import batch_size must be between 1 and 500.")
    if not scope:
        raise ValueError("Import scope is required.")
    saved = (
        checkpoint_store.load_import_checkpoint(source=source_name, scope=scope)
        if checkpoint_store
        else None
    )
    seen = proposed = duplicates = conflicts = rejected = 0
    current = checkpoint if checkpoint is not None else (saved or {}).get("checkpoint")
    while True:
        candidates, next_checkpoint = source.page(current, batch_size)
        if not candidates:
            break
        seen += len(candidates)
        if dry_run:
            current = next_checkpoint
            if next_checkpoint is None:
                break
            continue
        for candidate in candidates:
            try:
                result = submitter.submit_candidate(candidate)
            except (OSError, ValueError):
                rejected += 1
                continue
            assessment = result.get("assessment")
            if assessment == "duplicate":
                duplicates += 1
            elif assessment == "conflict":
                conflicts += 1
            else:
                proposed += 1
        current = next_checkpoint
        report = ImportReport(
            source_name,
            dry_run,
            seen,
            proposed,
            duplicates,
            conflicts,
            rejected,
            current,
        )
        if checkpoint_store:
            # The page is committed only after every candidate has been accounted for.
            checkpoint_store.save_import_checkpoint(
                source=source_name,
                scope=scope,
                checkpoint=current,
                report=report.as_dict(),
            )
        if next_checkpoint is None:
            break
    return ImportReport(
        source=source_name,
        dry_run=dry_run,
        seen=seen,
        proposed=proposed,
        duplicates=duplicates,
        conflicts=conflicts,
        rejected=rejected,
        checkpoint=current,
    )
