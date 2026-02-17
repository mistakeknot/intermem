"""Tests for stability detection via per-entry content hashing."""
from __future__ import annotations

from intermem.scanner import MemoryEntry
from intermem.stability import StabilityStore, record_snapshot, score_entries


def _entry(content: str, section: str = "Test") -> MemoryEntry:
    return MemoryEntry(
        content=content,
        section=section,
        source_file="MEMORY.md",
        start_line=1,
        end_line=1,
    )


def test_first_run_creates_baseline(tmp_path):
    """First snapshot records entries but scores all as recent."""
    store = StabilityStore(tmp_path / ".intermem" / "stability.jsonl")
    entries = [_entry("- Fact A"), _entry("- Fact B")]
    record_snapshot(store, entries)

    scores = score_entries(store, entries)
    assert all(score.score == "recent" for score in scores)
    assert store.path.exists()


def test_stable_after_three_snapshots(tmp_path):
    """Entry unchanged across 3 snapshots scores stable."""
    store = StabilityStore(tmp_path / ".intermem" / "stability.jsonl")
    entries = [_entry("- Fact A", section="Workflow")]

    for _ in range(3):
        record_snapshot(store, entries)

    scores = score_entries(store, entries)
    assert scores[0].score == "stable"


def test_volatile_when_content_changes(tmp_path):
    """Changed content in the same section scores volatile."""
    store = StabilityStore(tmp_path / ".intermem" / "stability.jsonl")
    entries_v1 = [_entry("- Fact A version 1", section="Oracle CLI")]
    entries_v2 = [_entry("- Fact A version 2", section="Oracle CLI")]

    record_snapshot(store, entries_v1)
    record_snapshot(store, entries_v1)
    record_snapshot(store, entries_v2)

    scores = score_entries(store, entries_v2)
    assert scores[0].score == "volatile"


def test_new_entry_scores_recent_when_section_has_no_prior_hashes(tmp_path):
    """New entry with count==1 and no prior hash in same section is recent."""
    store = StabilityStore(tmp_path / ".intermem" / "stability.jsonl")
    old = [_entry("- Old fact", section="Legacy")]
    record_snapshot(store, old)
    record_snapshot(store, old)

    new_entries = [
        _entry("- Old fact", section="Legacy"),
        _entry("- Brand new", section="New Section"),
    ]
    record_snapshot(store, new_entries)

    scores = score_entries(store, new_entries)
    assert scores[0].score == "stable"
    assert scores[1].score == "recent"


def test_new_hash_in_existing_section_scores_volatile(tmp_path):
    """New hash with count==1 in section with prior different hash is volatile."""
    store = StabilityStore(tmp_path / ".intermem" / "stability.jsonl")
    baseline = [_entry("- Existing guidance", section="Git Workflow")]
    record_snapshot(store, baseline)
    record_snapshot(store, baseline)

    changed = [_entry("- Existing guidance (rewritten)", section="Git Workflow")]
    record_snapshot(store, changed)

    scores = score_entries(store, changed)
    assert scores[0].score == "volatile"


def test_removed_entry_not_in_scores(tmp_path):
    """Entries not in current scan are not scored."""
    store = StabilityStore(tmp_path / ".intermem" / "stability.jsonl")
    entries = [_entry("- Fact A"), _entry("- Fact B")]
    record_snapshot(store, entries)

    current = [_entry("- Fact A")]
    record_snapshot(store, current)
    scores = score_entries(store, current)

    assert len(scores) == 1


def test_store_persists_across_instances(tmp_path):
    """Store data survives creating a new StabilityStore for the same file."""
    path = tmp_path / ".intermem" / "stability.jsonl"
    store1 = StabilityStore(path)
    record_snapshot(store1, [_entry("- Fact A")])

    store2 = StabilityStore(path)
    record_snapshot(store2, [_entry("- Fact A")])

    store3 = StabilityStore(path)
    record_snapshot(store3, [_entry("- Fact A")])
    scores = score_entries(store3, [_entry("- Fact A")])
    assert scores[0].score == "stable"

