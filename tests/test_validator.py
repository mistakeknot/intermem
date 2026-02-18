"""Tests for validator — validate_and_filter_entries + validate_promoted + decay + sweep."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from intermem.metadata import MetadataStore, STALE_THRESHOLD
from intermem.scanner import MemoryEntry
from intermem.validator import (
    DEMOTION_STREAK_THRESHOLD,
    SweepResult,
    ValidationResult,
    apply_decay_penalty,
    sweep_all_entries,
    validate_and_filter_entries,
    validate_promoted,
)


def _entry(content: str, section: str = "Test", source_file: str = "test.md") -> MemoryEntry:
    return MemoryEntry(
        content=content,
        section=section,
        source_file=source_file,
        start_line=1,
        end_line=1,
    )


@pytest.fixture
def store(tmp_path):
    return MetadataStore(tmp_path / ".intermem" / "metadata.db")


# -- validate_and_filter_entries --


class TestValidateAndFilter:
    def test_all_valid_citations_pass_through(self, tmp_path, store):
        """Entries with valid citations remain in the output."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").touch()

        entry = _entry("- Edit `src/main.py` for changes")
        result = validate_and_filter_entries([entry], store, tmp_path)

        assert result.validated_count == 1
        assert result.stale_count == 0
        assert len(result.validated_entries) == 1
        assert len(result.stale_filtered) == 0

    def test_broken_citation_filters_entry(self, tmp_path, store):
        """Entry with broken citation gets filtered as stale."""
        entry = _entry("- Edit `src/nonexistent.py` and `lib/gone.py` for changes")
        result = validate_and_filter_entries([entry], store, tmp_path)

        assert result.validated_count == 1
        assert result.stale_count == 1
        assert len(result.validated_entries) == 0
        assert len(result.stale_filtered) == 1

    def test_no_citations_keeps_base_confidence(self, tmp_path, store):
        """Entry with no extractable citations keeps base confidence (0.5) and passes."""
        entry = _entry("- Always use WAL mode for SQLite")
        result = validate_and_filter_entries([entry], store, tmp_path)

        assert result.validated_count == 1
        assert result.stale_count == 0
        assert len(result.validated_entries) == 1

    def test_mixed_entries(self, tmp_path, store):
        """Mix of valid, broken, and no-citation entries."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "existing.py").touch()

        entries = [
            _entry("- Valid: `src/existing.py`"),
            _entry("- Broken: `src/missing.py` and `lib/gone.py`"),
            _entry("- No citations: just a fact"),
        ]
        result = validate_and_filter_entries(entries, store, tmp_path)

        assert result.validated_count == 3
        assert result.stale_count == 1  # broken one
        assert len(result.validated_entries) == 2  # valid + no-citations

    def test_empty_entries(self, tmp_path, store):
        result = validate_and_filter_entries([], store, tmp_path)
        assert result.validated_count == 0
        assert result.stale_count == 0

    def test_metadata_stored(self, tmp_path, store):
        """Validation records entries and citations in metadata.db."""
        (tmp_path / "file.py").touch()
        entry = _entry("- Check `file.py` works")
        validate_and_filter_entries([entry], store, tmp_path)

        # Verify metadata was persisted.
        from intermem._util import hash_entry
        entry_hash = hash_entry(entry)
        row = store.get_entry(entry_hash)
        assert row is not None
        assert row["confidence"] >= 0.5

    def test_transaction_rollback_on_error(self, tmp_path):
        """If validation raises, transaction is rolled back."""
        store = MetadataStore(tmp_path / ".intermem" / "metadata.db")

        # Monkeypatch to force an error during validation.
        original_upsert = store.upsert_entry
        call_count = 0

        def failing_upsert(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise RuntimeError("simulated crash")
            return original_upsert(*args, **kwargs)

        store.upsert_entry = failing_upsert

        entries = [
            _entry("- First entry"),
            _entry("- Second entry"),
        ]

        with pytest.raises(RuntimeError, match="simulated crash"):
            validate_and_filter_entries(entries, store, tmp_path)

        # Transaction rolled back — no entries should be committed.
        from intermem._util import hash_entry
        for entry in entries:
            assert store.get_entry(hash_entry(entry)) is None


# -- Re-promotion of demoted entries --


class TestRepromotion:
    """Tests for demoted entries re-entering the validation pipeline."""

    def test_demoted_entry_repromotion(self, tmp_path, store):
        """Demoted entry reappears with good citations → reactivated."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").touch()

        entry = _entry("- Edit `src/main.py` for changes")
        from intermem._util import hash_entry
        h = hash_entry(entry)

        # Pre-create as demoted
        store.upsert_entry(h, "preview", "Test", "test.md")
        store.mark_demoted(h)
        store.conn.commit()
        assert store.get_entry(h)["status"] == "demoted"

        # Re-enter pipeline with valid citation
        result = validate_and_filter_entries([entry], store, tmp_path)

        row = store.get_entry(h)
        assert row["status"] == "active"
        assert row["stale_streak"] == 0
        assert len(result.validated_entries) == 1

    def test_demoted_entry_stays_demoted_if_stale(self, tmp_path, store):
        """Demoted entry reappears with broken citations → stays demoted."""
        entry = _entry("- Edit `src/nonexistent.py` and `lib/gone.py` for changes")
        from intermem._util import hash_entry
        h = hash_entry(entry)

        # Pre-create as demoted
        store.upsert_entry(h, "preview", "Test", "test.md")
        store.mark_demoted(h)
        store.conn.commit()

        # Re-enter pipeline with broken citations
        result = validate_and_filter_entries([entry], store, tmp_path)

        row = store.get_entry(h)
        assert row["status"] == "demoted"

    def test_demoted_entry_no_citations_repromates(self, tmp_path, store):
        """Demoted entry with no citations keeps base confidence (0.5) → reactivated."""
        entry = _entry("- Always use WAL mode for SQLite")
        from intermem._util import hash_entry
        h = hash_entry(entry)

        store.upsert_entry(h, "preview", "Test", "test.md")
        store.mark_demoted(h)
        store.conn.commit()

        result = validate_and_filter_entries([entry], store, tmp_path)

        row = store.get_entry(h)
        assert row["status"] == "active"  # 0.5 >= STALE_THRESHOLD (0.3)

    def test_full_lifecycle_decay_to_repromotion(self, tmp_path, store):
        """Full round-trip: validate → sweep/decay → demote → reappear → re-promote."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").touch()

        entry = _entry("- Edit `src/main.py` for changes")
        from intermem._util import hash_entry
        h = hash_entry(entry)

        # Step 1: Initial validation (active, good confidence)
        validate_and_filter_entries([entry], store, tmp_path)
        assert store.get_entry(h)["status"] == "active"

        # Step 2: Simulate demotion (as if sweep found it stale)
        store.mark_demoted(h)
        store.conn.commit()
        assert store.get_entry(h)["status"] == "demoted"

        # Step 3: Entry reappears in auto-memory → re-promotion
        result = validate_and_filter_entries([entry], store, tmp_path)
        row = store.get_entry(h)
        assert row["status"] == "active"
        assert row["stale_streak"] == 0
        assert row["demoted_at"] is None
        assert len(result.validated_entries) == 1


# -- validate_promoted --


class TestValidatePromoted:
    def test_valid_promoted_entries(self, tmp_path, store):
        """Promoted entries with valid citations report as healthy."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").touch()

        target = tmp_path / "AGENTS.md"
        target.write_text(
            "# AGENTS\n\n"
            "## Architecture\n"
            "- Edit `src/main.py` for changes <!-- intermem -->\n"
        )

        result = validate_promoted([target], store, tmp_path)
        assert result.entries_checked == 1
        assert result.entries_healthy == 1
        assert result.entries_stale == 0
        assert result.reports[0].citations_valid == 1
        assert result.reports[0].citations_broken == 0

    def test_stale_promoted_entry(self, tmp_path, store):
        """Promoted entry with broken citation reported as stale."""
        target = tmp_path / "AGENTS.md"
        target.write_text(
            "# AGENTS\n\n"
            "## Architecture\n"
            "- Edit `src/deleted.py` and `lib/gone.py` for changes <!-- intermem -->\n"
        )

        result = validate_promoted([target], store, tmp_path)
        assert result.entries_checked == 1
        assert result.entries_stale == 1
        assert result.entries_healthy == 0
        assert len(result.reports[0].broken_details) == 2

    def test_missing_target_doc(self, tmp_path, store):
        """Missing target doc gracefully returns empty result."""
        result = validate_promoted([tmp_path / "nonexistent.md"], store, tmp_path)
        assert result.entries_checked == 0

    def test_no_intermem_markers(self, tmp_path, store):
        """Doc with no intermem markers returns empty result."""
        target = tmp_path / "AGENTS.md"
        target.write_text("# AGENTS\n\n## Section\n- Regular line\n")
        result = validate_promoted([target], store, tmp_path)
        assert result.entries_checked == 0

    def test_multiple_promoted_entries(self, tmp_path, store):
        """Multiple promoted entries validated independently."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "good.py").touch()

        target = tmp_path / "AGENTS.md"
        target.write_text(
            "# AGENTS\n\n"
            "## Section\n"
            "- Uses `src/good.py` for things <!-- intermem -->\n"
            "- Uses `src/bad.py` and `lib/gone.py` which are missing <!-- intermem -->\n"
        )

        result = validate_promoted([target], store, tmp_path)
        assert result.entries_checked == 2
        assert result.entries_healthy == 1
        assert result.entries_stale == 1

    def test_hash_based_marker(self, tmp_path, store):
        """New hash-based markers <!-- intermem:HASH --> are extracted correctly."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").touch()

        target = tmp_path / "AGENTS.md"
        target.write_text(
            "# AGENTS\n\n"
            "## Section\n"
            "- Uses `src/main.py` for things <!-- intermem:abc123def456 -->\n"
        )

        result = validate_promoted([target], store, tmp_path)
        assert result.entries_checked == 1
        assert result.entries_healthy == 1


# -- apply_decay_penalty --


class TestDecayPenalty:
    """Tests for the time-based confidence decay function."""

    _BASE = datetime(2026, 2, 18, tzinfo=timezone.utc)

    def test_within_14_days_no_penalty(self):
        """No decay during the 14-day grace period."""
        last_seen = (self._BASE - timedelta(days=10)).isoformat()
        result = apply_decay_penalty(0.8, last_seen, self._BASE)
        assert result == 0.8

    def test_exactly_14_days_no_penalty(self):
        """Day 14 is still within grace period."""
        last_seen = (self._BASE - timedelta(days=14)).isoformat()
        result = apply_decay_penalty(0.8, last_seen, self._BASE)
        assert result == 0.8

    def test_at_15_days_minus_0_1(self):
        """Day 15 enters first penalty period: -0.1."""
        last_seen = (self._BASE - timedelta(days=15)).isoformat()
        result = apply_decay_penalty(0.8, last_seen, self._BASE)
        assert result == pytest.approx(0.7)

    def test_at_28_days_still_minus_0_1(self):
        """Day 28 is still in first penalty period: -0.1 (off-by-one fix)."""
        last_seen = (self._BASE - timedelta(days=28)).isoformat()
        result = apply_decay_penalty(0.8, last_seen, self._BASE)
        assert result == pytest.approx(0.7)

    def test_at_29_days_minus_0_2(self):
        """Day 29 enters second penalty period: -0.2."""
        last_seen = (self._BASE - timedelta(days=29)).isoformat()
        result = apply_decay_penalty(0.8, last_seen, self._BASE)
        assert result == pytest.approx(0.6)

    def test_at_42_days_still_minus_0_2(self):
        """Day 42 is the end of second penalty period: -0.2."""
        last_seen = (self._BASE - timedelta(days=42)).isoformat()
        result = apply_decay_penalty(0.8, last_seen, self._BASE)
        assert result == pytest.approx(0.6)

    def test_at_43_days_minus_0_3(self):
        """Day 43 enters third penalty period: -0.3."""
        last_seen = (self._BASE - timedelta(days=43)).isoformat()
        result = apply_decay_penalty(0.8, last_seen, self._BASE)
        assert result == pytest.approx(0.5)

    def test_clamps_to_zero(self):
        """Very old entry doesn't go negative."""
        last_seen = (self._BASE - timedelta(days=365)).isoformat()
        result = apply_decay_penalty(0.5, last_seen, self._BASE)
        assert result == 0.0

    def test_naive_last_seen_timestamp(self):
        """Naive (no TZ) last_seen is treated as UTC without TypeError."""
        # SQLite datetime('now') produces naive timestamps
        naive_ts = "2026-02-01 12:00:00"
        result = apply_decay_penalty(0.8, naive_ts, self._BASE)
        # 17 days → first penalty period → -0.1
        assert result == pytest.approx(0.7)

    def test_aware_last_seen_timestamp(self):
        """UTC-aware last_seen works correctly."""
        aware_ts = (self._BASE - timedelta(days=15)).isoformat()
        result = apply_decay_penalty(0.8, aware_ts, self._BASE)
        assert result == pytest.approx(0.7)

    def test_corrupt_timestamp_returns_zero(self):
        """Invalid timestamp string returns 0.0 (maximally stale)."""
        result = apply_decay_penalty(0.8, "not-a-date", self._BASE)
        assert result == 0.0


# -- sweep_all_entries --


class TestSweep:
    """Tests for the full sweep across all entries."""

    def test_sweep_no_decay_fresh_entries(self, tmp_path, store):
        """Fresh entries (just created) are not decayed."""
        store.upsert_entry("h1", "fact 1", "sec", "f.md")
        store.conn.commit()

        result = sweep_all_entries(store, tmp_path)
        assert result.entries_swept == 1
        assert result.entries_decayed == 0

    def test_sweep_with_decay(self, tmp_path, store):
        """Old entries get confidence penalty."""
        store.upsert_entry("h1", "old fact", "sec", "f.md")
        # Backdate last_seen to 30 days ago
        store.conn.execute(
            "UPDATE memory_entries SET last_seen = datetime('now', '-30 days') WHERE entry_hash = 'h1'"
        )
        store.conn.commit()

        result = sweep_all_entries(store, tmp_path)
        assert result.entries_swept == 1
        assert result.entries_decayed == 1

        entry = store.get_entry("h1")
        assert entry["confidence"] < 0.5  # Base 0.5 minus decay penalty

    def test_sweep_stale_streak_increment(self, tmp_path, store):
        """Entry below stale threshold gets stale_streak incremented."""
        store.upsert_entry("h1", "stale fact", "sec", "f.md")
        # Backdate to trigger decay below STALE_THRESHOLD
        store.conn.execute(
            "UPDATE memory_entries SET last_seen = datetime('now', '-90 days') WHERE entry_hash = 'h1'"
        )
        store.conn.commit()

        sweep_all_entries(store, tmp_path)
        entry = store.get_entry("h1")
        assert entry["stale_streak"] >= 1

    def test_sweep_stale_streak_reset(self, tmp_path, store):
        """Healthy entry resets stale_streak to 0."""
        store.upsert_entry("h1", "good fact", "sec", "f.md")
        store.increment_stale_streak("h1")
        store.conn.commit()
        assert store.get_entry("h1")["stale_streak"] == 1

        # Fresh last_seen → no decay → confidence stays at 0.5 → healthy
        result = sweep_all_entries(store, tmp_path)
        entry = store.get_entry("h1")
        assert entry["stale_streak"] == 0

    def test_sweep_single_transaction_rollback(self, tmp_path):
        """If sweep crashes mid-way, no partial updates are committed."""
        store = MetadataStore(tmp_path / ".intermem" / "metadata.db")
        store.upsert_entry("h1", "fact 1", "sec", "f.md")
        store.upsert_entry("h2", "fact 2", "sec", "f.md")
        store.conn.commit()
        original_h1 = store.get_entry("h1")["confidence"]

        # Monkeypatch to crash after processing first entry
        original_increment = store.increment_stale_streak
        call_count = 0

        def crashing_increment(entry_hash):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise RuntimeError("simulated crash")
            return original_increment(entry_hash)

        store.increment_stale_streak = crashing_increment
        # Backdate both entries to trigger stale path
        store.conn.execute(
            "UPDATE memory_entries SET last_seen = datetime('now', '-90 days')"
        )
        store.conn.commit()

        with pytest.raises(RuntimeError, match="simulated crash"):
            sweep_all_entries(store, tmp_path)

        # h1's confidence should be unchanged (transaction rolled back)
        entry = store.get_entry("h1")
        assert entry["confidence"] == original_h1

    def test_sweep_skips_orphaned_entries(self, tmp_path, store):
        """Orphaned entries are not processed during sweep."""
        store.upsert_entry("h1", "active fact", "sec", "f.md")
        store.upsert_entry("h2", "orphaned fact", "sec", "f.md")
        store.conn.execute(
            "UPDATE memory_entries SET status = 'orphaned' WHERE entry_hash = 'h2'"
        )
        store.conn.commit()

        result = sweep_all_entries(store, tmp_path)
        assert result.entries_swept == 1  # Only h1

    def test_sweep_skips_demoted_entries(self, tmp_path, store):
        """Demoted entries are not processed during sweep."""
        store.upsert_entry("h1", "active fact", "sec", "f.md")
        store.upsert_entry("h2", "demoted fact", "sec", "f.md")
        store.mark_demoted("h2")
        store.conn.commit()

        result = sweep_all_entries(store, tmp_path)
        assert result.entries_swept == 1  # Only h1

    def test_sweep_corrupt_last_seen(self, tmp_path, store):
        """Entry with corrupt last_seen doesn't crash (handled by decay penalty)."""
        store.upsert_entry("h1", "fact", "sec", "f.md")
        store.conn.execute(
            "UPDATE memory_entries SET last_seen = 'not-a-date' WHERE entry_hash = 'h1'"
        )
        store.conn.commit()

        # Should not raise — apply_decay_penalty returns 0.0 for corrupt timestamps
        result = sweep_all_entries(store, tmp_path)
        assert result.entries_swept == 1
        # Corrupt timestamp → 0.0 confidence → stale
        entry = store.get_entry("h1")
        assert entry["confidence"] == 0.0

    def test_sweep_returns_demotion_candidates(self, tmp_path, store):
        """Entries with stale_streak >= threshold are listed as candidates."""
        store.upsert_entry("h1", "good", "sec", "f.md")
        store.upsert_entry("h2", "stale", "sec", "f.md")
        # Pre-set h2 with streak at threshold - 1 and backdate
        store.increment_stale_streak("h2")
        store.conn.execute(
            "UPDATE memory_entries SET last_seen = datetime('now', '-90 days') "
            "WHERE entry_hash = 'h2'"
        )
        store.conn.commit()

        # After sweep, h2's streak should reach threshold
        result = sweep_all_entries(store, tmp_path)
        assert "h2" in result.demotion_candidates
        assert "h1" not in result.demotion_candidates

    def test_sweep_crash_recovery_from_journal(self, tmp_path, store):
        """Incomplete demotion in journal is reconciled at sweep start."""
        from intermem.journal import PromotionJournal

        journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
        store.upsert_entry("h1", "fact", "sec", "f.md")
        store.conn.commit()

        # Simulate incomplete demotion: journal says demoted, DB says active
        journal.record_demoted("h1", "AGENTS.md", "Section", "fact content")

        result = sweep_all_entries(store, tmp_path, journal=journal)

        # After crash recovery, h1 should be marked demoted in DB
        entry = store.get_entry("h1")
        assert entry["status"] == "demoted"
        # Demoted entries are skipped in sweep count
        assert result.entries_swept == 0
