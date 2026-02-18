"""Tests for validator — validate_and_filter_entries + validate_promoted."""
from __future__ import annotations

from pathlib import Path

import pytest

from intermem.metadata import MetadataStore
from intermem.scanner import MemoryEntry
from intermem.validator import (
    ValidationResult,
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
