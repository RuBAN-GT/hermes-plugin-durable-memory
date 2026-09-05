from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from hermes_durable_memory.config import Settings
from hermes_durable_memory.importer import import_candidates
from hermes_durable_memory.importers import HolographicSQLiteSource
from hermes_durable_memory.models import CommandError, MemoryCandidate
from hermes_durable_memory.policies import ApprovalPolicy
from hermes_durable_memory.service import DurableMemory
from hermes_durable_memory.store import InMemoryStore


@dataclass(frozen=True, slots=True)
class _RejectingSubmitter:
    error: OSError | ValueError

    def submit_candidate(self, candidate: MemoryCandidate) -> dict[str, object]:
        raise self.error


class HolographicImportTests(unittest.TestCase):
    def _source(self, root: Path) -> HolographicSQLiteSource:
        database = root / "source.db"
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "CREATE TABLE facts (fact_id INTEGER PRIMARY KEY, content TEXT, "
                "category TEXT, tags TEXT, trust_score REAL, created_at TEXT)"
            )
            connection.executemany(
                "INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        1,
                        "Ada likes compilers",
                        "preference",
                        "ada,compiler",
                        0.9,
                        "2026-01-01 10:00:00",
                    ),
                    (
                        2,
                        "Ada reads science fiction",
                        "preference",
                        "ada,books",
                        0.8,
                        "2026-01-02 10:00:00",
                    ),
                ],
            )
            connection.commit()
        finally:
            connection.close()
        return HolographicSQLiteSource(database)

    def test_dry_run_is_read_only_and_import_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self._source(Path(directory))
            memory = DurableMemory(
                settings=Settings(
                    store="memory",
                    profile="alpha",
                    policy=ApprovalPolicy(create="auto"),
                ),
                store=InMemoryStore(),
            )
            dry_run = import_candidates(
                memory, source, source_name=source.source_name, dry_run=True
            )
            self.assertEqual(dry_run.seen, 2)
            self.assertEqual(memory.search("Ada")["records"], [])

            first = import_candidates(
                memory, source, source_name=source.source_name, batch_size=1
            )
            second = import_candidates(memory, source, source_name=source.source_name)

            self.assertEqual(first.proposed, 2)
            self.assertEqual(second.duplicates, 2)
            self.assertEqual(len(memory.search("Ada")["records"]), 2)

    def test_checkpoint_resumes_after_a_committed_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self._source(Path(directory))
            memory = DurableMemory(
                settings=Settings(
                    store="memory",
                    profile="alpha",
                    policy=ApprovalPolicy(create="auto"),
                ),
                store=InMemoryStore(),
            )
            first = import_candidates(
                memory, source, source_name=source.source_name, batch_size=1
            )
            resumed = import_candidates(
                memory,
                source,
                source_name=source.source_name,
                checkpoint=first.checkpoint,
            )

            self.assertEqual(first.checkpoint, "2")
            self.assertEqual(resumed.seen, 0)

    def test_checkpoint_and_reconciliation_are_persisted_after_each_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self._source(Path(directory))
            memory = DurableMemory(
                settings=Settings(
                    store="memory",
                    profile="alpha",
                    policy=ApprovalPolicy(create="auto"),
                ),
                store=InMemoryStore(),
            )
            report = import_candidates(
                memory,
                source,
                source_name=source.source_name,
                batch_size=1,
                checkpoint_store=memory.store(),
                scope="profile:alpha",
            )
            saved = memory.store().load_import_checkpoint(
                source=source.source_name, scope="profile:alpha"
            )
            self.assertEqual(saved, {"checkpoint": "2", "report": report.as_dict()})
            self.assertTrue(saved["report"]["explained"])

    def test_transient_failure_does_not_advance_checkpoint(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            source = self._source(Path(directory))
            store = InMemoryStore()
            raised = False

            # When
            try:
                import_candidates(
                    _RejectingSubmitter(OSError("temporary source failure")),
                    source,
                    source_name=source.source_name,
                    checkpoint_store=store,
                    scope="profile:alpha",
                )
            except OSError:
                raised = True

            # Then
            saved = store.load_import_checkpoint(
                source=source.source_name, scope="profile:alpha"
            )
            self.assertEqual((raised, saved), (True, None))

    def test_command_error_does_not_advance_checkpoint(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            source = self._source(Path(directory))
            store = InMemoryStore()

            # When / Then
            with self.assertRaises(CommandError):
                import_candidates(
                    _RejectingSubmitter(CommandError("database unavailable")),
                    source,
                    source_name=source.source_name,
                    checkpoint_store=store,
                    scope="profile:alpha",
                )
            self.assertIsNone(
                store.load_import_checkpoint(
                    source=source.source_name, scope="profile:alpha"
                )
            )

    def test_value_error_remains_counted_rejection(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            source = self._source(Path(directory))
            store = InMemoryStore()

            # When
            report = import_candidates(
                _RejectingSubmitter(ValueError("invalid candidate")),
                source,
                source_name=source.source_name,
                checkpoint_store=store,
                scope="profile:alpha",
            )

            # Then
            saved = store.load_import_checkpoint(
                source=source.source_name, scope="profile:alpha"
            )
            self.assertEqual(
                (report.seen, report.rejected, report.checkpoint, saved),
                (2, 2, "2", {"checkpoint": "2", "report": report.as_dict()}),
            )


if __name__ == "__main__":
    unittest.main()
