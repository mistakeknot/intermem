"""Tests for the promoter — writes entries to AGENTS.md/CLAUDE.md + demotion."""
import re

import pytest

from intermem.journal import PromotionJournal
from intermem.metadata import MetadataStore
from intermem.promoter import DemotionResult, PromotionResult, demote_entries, promote_entries
from intermem.scanner import MemoryEntry
from intermem._util import hash_entry


def _entry(content: str, section: str = "Testing") -> MemoryEntry:
    return MemoryEntry(
        content=content,
        section=section,
        source_file="MEMORY.md",
        start_line=1,
        end_line=1,
    )


def test_promote_appends_to_matching_section(tmp_path):
    """Entry is appended under the matching ## section."""
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Project\n\n## Testing\n- Existing fact\n\n## Build\n- Build stuff\n")
    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    entry = _entry("- New testing fact", section="Testing")

    result = promote_entries([entry], agents, journal)

    assert isinstance(result, PromotionResult)
    content = agents.read_text()
    assert "- New testing fact" in content
    testing_idx = content.index("## Testing")
    build_idx = content.index("## Build")
    new_fact_idx = content.index("- New testing fact")
    assert testing_idx < new_fact_idx < build_idx
    assert result.promoted_count == 1


def test_promote_adds_hash_marker_comment(tmp_path):
    """Promoted entries have <!-- intermem:HASH --> marker with entry hash."""
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Project\n\n## Testing\n- Existing fact\n")
    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    entry = _entry("- New fact", section="Testing")

    promote_entries([entry], agents, journal)

    content = agents.read_text()
    import re
    assert re.search(r"<!-- intermem:[a-f0-9]+ -->", content)


def test_promote_creates_section_if_missing(tmp_path):
    """If target section doesn't exist, appends new section at end."""
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Project\n\n## Build\n- Build stuff\n")
    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    entry = _entry("- New testing fact", section="Testing")

    promote_entries([entry], agents, journal)

    content = agents.read_text()
    assert "## Testing" in content
    assert "- New testing fact" in content


def test_promote_journals_entries(tmp_path):
    """Each promoted entry is recorded in the journal as committed."""
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Project\n\n## Testing\n- Existing\n")
    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    entry = _entry("- New fact", section="Testing")

    promote_entries([entry], agents, journal)

    incomplete = journal.get_incomplete()
    assert len(incomplete) == 1
    assert incomplete[0].status == "committed"


def test_promote_creates_target_if_missing(tmp_path):
    """If target file doesn't exist, creates it with a heading."""
    agents = tmp_path / "AGENTS.md"
    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    entry = _entry("- New fact", section="Testing")

    promote_entries([entry], agents, journal)

    assert agents.exists()
    content = agents.read_text()
    assert "## Testing" in content
    assert "- New fact" in content


def test_promote_multiple_entries_same_section(tmp_path):
    """Multiple entries for the same section are grouped together."""
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Project\n\n## CLI\n- Existing\n")
    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    entries = [
        _entry("- Fact A", section="CLI"),
        _entry("- Fact B", section="CLI"),
    ]

    result = promote_entries(entries, agents, journal)

    content = agents.read_text()
    assert "- Fact A" in content
    assert "- Fact B" in content
    assert result.promoted_count == 2


# -- Phase 2A: Demotion --


@pytest.fixture
def store(tmp_path):
    return MetadataStore(tmp_path / ".intermem" / "metadata.db")


def test_promote_uses_hash_marker(tmp_path):
    """New promotions produce <!-- intermem:HASH --> markers."""
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Project\n\n## Testing\n- Existing\n")
    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    entry = _entry("- Hash fact", section="Testing")

    promote_entries([entry], agents, journal)

    content = agents.read_text()
    entry_h = hash_entry(entry)
    expected_marker = f"<!-- intermem:{entry_h[:8]} -->"
    assert expected_marker in content


def test_demote_removes_line_by_hash(tmp_path, store):
    """Hash-matched line is removed from the target doc, others preserved."""
    agents = tmp_path / "AGENTS.md"
    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")

    # Promote two entries
    e1 = _entry("- Keep this fact", section="Testing")
    e2 = _entry("- Remove this fact", section="Testing")
    promote_entries([e1, e2], agents, journal)

    # Register entries in metadata
    h2 = hash_entry(e2)
    store.upsert_entry(h2, "Remove this fact", "Testing", "f.md")
    store.conn.commit()

    result = demote_entries([h2], store, [agents], journal)

    assert result.demoted_count == 1
    content = agents.read_text()
    assert "- Keep this fact" in content
    assert "- Remove this fact" not in content


def test_demote_updates_metadata(tmp_path, store):
    """Demoted entry's status becomes 'demoted' in metadata."""
    agents = tmp_path / "AGENTS.md"
    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")

    entry = _entry("- Stale fact", section="Testing")
    promote_entries([entry], agents, journal)

    h = hash_entry(entry)
    store.upsert_entry(h, "Stale fact", "Testing", "f.md")
    store.conn.commit()

    demote_entries([h], store, [agents], journal)
    store.conn.commit()

    row = store.get_entry(h)
    assert row["status"] == "demoted"


def test_demote_records_journal(tmp_path, store):
    """Journal has 'demoted' then 'demotion_committed' entries."""
    agents = tmp_path / "AGENTS.md"
    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")

    entry = _entry("- Old fact", section="Testing")
    promote_entries([entry], agents, journal)

    h = hash_entry(entry)
    store.upsert_entry(h, "Old fact", "Testing", "f.md")
    store.conn.commit()

    demote_entries([h], store, [agents], journal)

    # All demotions should be committed (not unresolved)
    assert len(journal.get_unresolved_demotions()) == 0


def test_demote_old_style_marker_not_removed(tmp_path, store):
    """Old-style <!-- intermem --> markers are NOT accidentally demoted."""
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        "# Project\n\n"
        "## Testing\n"
        "- Old fact <!-- intermem -->\n"
    )
    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")

    # Try to demote with some hash that doesn't match any marker
    store.upsert_entry("abcdef1234567890", "Old fact", "Testing", "f.md")
    store.conn.commit()

    result = demote_entries(["abcdef1234567890"], store, [agents], journal)

    assert result.demoted_count == 0
    content = agents.read_text()
    assert "- Old fact <!-- intermem -->" in content


def test_demote_entry_not_in_any_doc(tmp_path, store):
    """Entry hash in DB but not in target docs is a graceful no-op."""
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Project\n\n## Testing\n- Some content\n")
    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")

    store.upsert_entry("hash_not_in_doc", "Phantom", "Testing", "f.md")
    store.conn.commit()

    result = demote_entries(["hash_not_in_doc"], store, [agents], journal)
    assert result.demoted_count == 0
    assert result.files_modified == []
