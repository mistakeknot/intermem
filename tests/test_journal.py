"""Tests for the promotion journal (WAL-style atomicity)."""
import json
from pathlib import Path
from intermem.journal import PromotionJournal, JournalEntry


def test_journal_creates_file(tmp_path):
    """Journal file is created on first write."""
    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    journal.record_pending("hash1", "AGENTS.md", "## Section", "- Fact content")
    assert journal.path.exists()


def test_journal_records_pending(tmp_path):
    """Pending entries are recorded with correct status."""
    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    journal.record_pending("hash1", "AGENTS.md", "## Section", "- Fact content")
    entries = journal.get_incomplete()
    assert len(entries) == 1
    assert entries[0].status == "pending"
    assert entries[0].entry_hash == "hash1"


def test_journal_marks_committed(tmp_path):
    """After promotion write succeeds, entry is marked committed."""
    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    journal.record_pending("hash1", "AGENTS.md", "## Section", "- Fact")
    journal.mark_committed("hash1")
    entries = journal.get_incomplete()
    # Committed but not yet pruned = still incomplete
    assert len(entries) == 1
    assert entries[0].status == "committed"


def test_journal_marks_pruned(tmp_path):
    """After prune succeeds, entry is marked pruned (complete)."""
    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    journal.record_pending("hash1", "AGENTS.md", "## Section", "- Fact")
    journal.mark_committed("hash1")
    journal.mark_pruned("hash1")
    entries = journal.get_incomplete()
    assert len(entries) == 0  # Fully complete


def test_journal_survives_reload(tmp_path):
    """Journal state persists across instances."""
    path = tmp_path / ".intermem" / "promotion-journal.jsonl"
    j1 = PromotionJournal(path)
    j1.record_pending("hash1", "AGENTS.md", "## Section", "- Fact")
    j1.mark_committed("hash1")

    j2 = PromotionJournal(path)
    entries = j2.get_incomplete()
    assert len(entries) == 1
    assert entries[0].status == "committed"


def test_journal_multiple_entries(tmp_path):
    """Multiple entries tracked independently."""
    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    journal.record_pending("hash1", "AGENTS.md", "## A", "- Fact 1")
    journal.record_pending("hash2", "CLAUDE.md", "## B", "- Fact 2")
    journal.mark_committed("hash1")
    journal.mark_pruned("hash1")
    incomplete = journal.get_incomplete()
    assert len(incomplete) == 1
    assert incomplete[0].entry_hash == "hash2"


# -- Phase 2A: Demotion journal --


def test_record_demoted(tmp_path):
    """Demoted status is accepted and persisted."""
    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    journal.record_demoted("hash1", "AGENTS.md", "Section", "- Old fact")
    demoted = journal.get_unresolved_demotions()
    assert len(demoted) == 1
    assert demoted[0].status == "demoted"
    assert demoted[0].entry_hash == "hash1"


def test_get_unresolved_demotions_excludes_committed(tmp_path):
    """Committed demotions are not returned as unresolved."""
    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    journal.record_demoted("hash1", "AGENTS.md", "Section", "- Fact")
    journal.mark_demotion_committed("hash1")
    demoted = journal.get_unresolved_demotions()
    assert len(demoted) == 0


def test_mark_demotion_committed(tmp_path):
    """Demotion committed pairs with the demoted entry."""
    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    journal.record_demoted("hash1", "AGENTS.md", "Section", "- Fact")
    journal.mark_demotion_committed("hash1")

    # Reload and verify
    j2 = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    assert len(j2.get_unresolved_demotions()) == 0
