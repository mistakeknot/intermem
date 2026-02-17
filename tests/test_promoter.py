"""Tests for the promoter — writes entries to AGENTS.md/CLAUDE.md."""
from intermem.journal import PromotionJournal
from intermem.promoter import PromotionResult, promote_entries
from intermem.scanner import MemoryEntry


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


def test_promote_adds_marker_comment(tmp_path):
    """Promoted entries have <!-- intermem --> marker."""
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Project\n\n## Testing\n- Existing fact\n")
    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    entry = _entry("- New fact", section="Testing")

    promote_entries([entry], agents, journal)

    content = agents.read_text()
    assert "<!-- intermem -->" in content


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
