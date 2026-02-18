"""Stability detection via per-entry content hashing across snapshots."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from intermem.scanner import MemoryEntry
from intermem._util import hash_entry


@dataclass
class StabilityScore:
    """Stability assessment for a single entry."""

    entry: MemoryEntry
    score: str  # "stable", "recent", "volatile"
    snapshot_count: int


class StabilityStore:
    """Persistent store for per-entry content hash history."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._snapshots: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                self._snapshots.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    def save_snapshot(self, snapshot: dict) -> None:
        """Persist a new snapshot to the store."""
        self._snapshots.append(snapshot)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(snapshot) + "\n")

    @property
    def snapshots(self) -> list[dict]:
        return self._snapshots


def _iter_snapshot_hash_sections(snapshot: dict) -> list[tuple[str, str]]:
    """Extract (hash, section) pairs from a snapshot.

    Supports the Task 3 format (`entries`) and a compatibility fallback (`hashes`).
    """
    pairs: list[tuple[str, str]] = []

    entries = snapshot.get("entries", [])
    if isinstance(entries, list) and entries:
        for item in entries:
            if not isinstance(item, dict):
                continue
            hash_value = item.get("hash")
            if not isinstance(hash_value, str) or not hash_value:
                continue
            section = item.get("section", "")
            section_value = section if isinstance(section, str) else str(section)
            pairs.append((hash_value, section_value))
        return pairs

    hashes = snapshot.get("hashes", {})
    if isinstance(hashes, dict):
        for hash_value, metadata in hashes.items():
            if not isinstance(hash_value, str) or not hash_value:
                continue
            section = ""
            if isinstance(metadata, dict):
                raw_section = metadata.get("section", "")
                section = raw_section if isinstance(raw_section, str) else str(raw_section)
            pairs.append((hash_value, section))
    return pairs


def record_snapshot(store: StabilityStore, entries: list[MemoryEntry]) -> None:
    """Record a new snapshot of current entries."""
    snapshot_entries: list[dict[str, str]] = []
    for entry in entries:
        entry_hash = hash_entry(entry)
        preview = entry.content[:80].replace("\n", " ")
        snapshot_entries.append(
            {
                "hash": entry_hash,
                "section": entry.section,
                "preview": preview,
            }
        )

    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entries": snapshot_entries,
    }
    store.save_snapshot(snapshot)


def score_entries(store: StabilityStore, entries: list[MemoryEntry]) -> list[StabilityScore]:
    """Score each entry's stability based on snapshot history.

    Rules:
    - stable: hash appears in 3+ snapshots
    - recent: hash appears in 0-2 snapshots, and for count==1 there is no prior different hash in the same section
    - volatile: hash appears once, and a different hash existed previously in that same section
    """
    snapshot_hashes: list[set[str]] = []
    section_hashes: list[dict[str, set[str]]] = []
    for snapshot in store.snapshots:
        hash_set: set[str] = set()
        section_map: dict[str, set[str]] = defaultdict(set)
        for hash_value, section in _iter_snapshot_hash_sections(snapshot):
            hash_set.add(hash_value)
            section_map[section].add(hash_value)
        snapshot_hashes.append(hash_set)
        section_hashes.append(section_map)

    scores: list[StabilityScore] = []
    for entry in entries:
        entry_hash = hash_entry(entry)
        seen_indices = [idx for idx, hashes in enumerate(snapshot_hashes) if entry_hash in hashes]
        count = len(seen_indices)

        if count >= 3:
            score = "stable"
        elif count == 1:
            first_seen_index = seen_indices[0]
            had_prior_hash_in_section = any(
                prior_hash != entry_hash
                for snapshot in section_hashes[:first_seen_index]
                for prior_hash in snapshot.get(entry.section, set())
            )
            score = "volatile" if had_prior_hash_in_section else "recent"
        else:
            score = "recent"

        scores.append(StabilityScore(entry=entry, score=score, snapshot_count=count))

    return scores

