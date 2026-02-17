"""Tests for deduplication checker against AGENTS.md/CLAUDE.md content."""

from intermem.dedup import check_duplicates
from intermem.scanner import MemoryEntry


def _entry(content: str, section: str = "Test") -> MemoryEntry:
    return MemoryEntry(
        content=content,
        section=section,
        source_file="MEMORY.md",
        start_line=1,
        end_line=1,
    )


def test_novel_entry_detected(tmp_path):
    """Entry with no match in target docs is marked novel."""
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Project\n\n## Build\n- Run `make build`\n")
    entry = _entry("- Always use pytest with -v flag")
    results = check_duplicates([entry], [agents])
    assert len(results) == 1
    assert results[0].status == "novel"


def test_exact_duplicate_detected(tmp_path):
    """Entry identical to existing content is marked exact_duplicate."""
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Project\n\n## Testing\n- Always use pytest with -v flag\n")
    entry = _entry("- Always use pytest with -v flag")
    results = check_duplicates([entry], [agents])
    assert results[0].status == "exact_duplicate"


def test_fuzzy_duplicate_detected(tmp_path):
    """Entry similar but not identical is marked fuzzy_duplicate with confidence."""
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Project\n\n## Testing\n- Always run pytest with the -v verbose flag\n")
    entry = _entry("- Always use pytest with -v flag")
    results = check_duplicates([entry], [agents])
    assert results[0].status == "fuzzy_duplicate"
    assert results[0].confidence > 0.7


def test_multiple_target_docs(tmp_path):
    """Checks against all provided target documents."""
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Agents\n\n## Build\n- Use make\n")
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("# Claude\n\n## Prefs\n- Always use pytest with -v flag\n")
    entry = _entry("- Always use pytest with -v flag")
    results = check_duplicates([entry], [agents, claude])
    assert results[0].status == "exact_duplicate"
    assert "CLAUDE.md" in results[0].matched_in


def test_missing_target_doc_skipped(tmp_path):
    """Non-existent target doc is gracefully skipped."""
    entry = _entry("- Some fact")
    results = check_duplicates([entry], [tmp_path / "nonexistent.md"])
    assert len(results) == 1
    assert results[0].status == "novel"


def test_dedup_result_has_match_context(tmp_path):
    """Fuzzy duplicate includes the matched line for user context."""
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Project\n\n## CLI\n- Oracle requires DISPLAY=:99 environment variable\n")
    entry = _entry("- Oracle CLI requires DISPLAY=:99")
    results = check_duplicates([entry], [agents])
    assert results[0].matched_line is not None
    assert "Oracle" in results[0].matched_line


def test_marker_is_stripped_before_comparing(tmp_path):
    """Intermem marker comment in target docs does not block exact matching."""
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Project\n\n## Testing\n- Always use pytest with -v flag <!-- intermem -->\n")
    entry = _entry("- Always use pytest with -v flag")
    results = check_duplicates([entry], [agents])
    assert len(results) == 1
    assert results[0].status == "exact_duplicate"
