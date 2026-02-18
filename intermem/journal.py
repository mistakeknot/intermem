"""WAL-style promotion journal for atomic promote+prune operations."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class JournalEntry:
    """A single promotion journal record."""

    entry_hash: str
    target_file: str
    target_section: str
    content: str
    status: str  # "pending", "committed", "pruned"
    timestamp: str


class PromotionJournal:
    """Append-only journal tracking promotion lifecycle.

    Lifecycle: pending -> committed (written to target) -> pruned (removed from source)

    On crash recovery, incomplete entries (pending or committed) can be
    replayed or discarded.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, JournalEntry] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            entry = JournalEntry(**data)
            self._entries[entry.entry_hash] = entry

    def _append(self, entry: JournalEntry) -> None:
        self._entries[entry.entry_hash] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(
                json.dumps(
                    {
                        "entry_hash": entry.entry_hash,
                        "target_file": entry.target_file,
                        "target_section": entry.target_section,
                        "content": entry.content,
                        "status": entry.status,
                        "timestamp": entry.timestamp,
                    }
                )
                + "\n"
            )

    def record_pending(
        self, entry_hash: str, target_file: str, target_section: str, content: str
    ) -> None:
        """Record that an entry is about to be promoted."""
        existing = self._entries.get(entry_hash)
        if existing is not None and existing.status in {"pending", "committed"}:
            return

        self._append(
            JournalEntry(
                entry_hash=entry_hash,
                target_file=target_file,
                target_section=target_section,
                content=content,
                status="pending",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

    def mark_committed(self, entry_hash: str) -> None:
        """Mark an entry as successfully written to target doc."""
        entry = self._entries.get(entry_hash)
        if entry is None:
            return
        updated = JournalEntry(
            entry_hash=entry.entry_hash,
            target_file=entry.target_file,
            target_section=entry.target_section,
            content=entry.content,
            status="committed",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self._append(updated)

    def mark_pruned(self, entry_hash: str) -> None:
        """Mark an entry as successfully pruned from source."""
        entry = self._entries.get(entry_hash)
        if entry is None:
            return
        updated = JournalEntry(
            entry_hash=entry.entry_hash,
            target_file=entry.target_file,
            target_section=entry.target_section,
            content=entry.content,
            status="pruned",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self._append(updated)

    def get_incomplete(self) -> list[JournalEntry]:
        """Return entries that haven't completed the full lifecycle."""
        return [e for e in self._entries.values() if e.status != "pruned"]

    # -- Phase 2A: Demotion journal --

    def record_demoted(
        self, entry_hash: str, target_file: str, target_section: str, content: str
    ) -> None:
        """Record that an entry has been demoted (removed from target doc)."""
        self._append(
            JournalEntry(
                entry_hash=entry_hash,
                target_file=target_file,
                target_section=target_section,
                content=content,
                status="demoted",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

    def get_unresolved_demotions(self) -> list[JournalEntry]:
        """Return demoted entries that haven't been committed to the DB yet.

        Used for crash recovery: if journal says demoted but DB disagrees,
        the sweep reconciles.
        """
        return [e for e in self._entries.values() if e.status == "demoted"]

    def mark_demotion_committed(self, entry_hash: str) -> None:
        """Mark a demotion as fully reconciled with the database."""
        entry = self._entries.get(entry_hash)
        if entry is None or entry.status != "demoted":
            return
        updated = JournalEntry(
            entry_hash=entry.entry_hash,
            target_file=entry.target_file,
            target_section=entry.target_section,
            content=entry.content,
            status="demotion_committed",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self._append(updated)
