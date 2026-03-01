"""Tests for auto-memory structural tidy."""

import textwrap
from pathlib import Path

from intermem.tidy import (
    Extraction,
    StaleCount,
    TidyResult,
    _slugify,
    detect_stale_counts,
    tidy_memory,
)


def _write_memory(tmp_path: Path, content: str) -> Path:
    """Write a MEMORY.md file and return the directory."""
    (tmp_path / "MEMORY.md").write_text(textwrap.dedent(content))
    return tmp_path


# --- Slugification ---


def test_slugify_simple():
    assert _slugify("Oracle CLI") == "oracle-cli"


def test_slugify_special_chars():
    assert _slugify("Git & Workflow (Tips)") == "git-workflow-tips"


def test_slugify_already_slug():
    assert _slugify("git-workflow") == "git-workflow"


def test_slugify_empty():
    assert _slugify("") == "untitled"


def test_slugify_unicode():
    assert _slugify("Über Étude") == "ber-tude"


# --- Under-budget behavior ---


def test_under_budget_returns_early(tmp_path):
    """MEMORY.md under budget returns under_budget=True with no extractions."""
    _write_memory(tmp_path, """\
        # Memory

        ## Quick Reference
        - fact one
        - fact two
    """)
    result = tidy_memory(tmp_path, budget=120)
    assert result.under_budget is True
    assert result.extractions == []
    assert result.total_lines == 5


def test_no_memory_file(tmp_path):
    """Missing MEMORY.md returns empty result."""
    result = tidy_memory(tmp_path)
    assert result.under_budget is True
    assert result.total_lines == 0


# --- Section extraction ---


def _make_large_memory(tmp_path: Path, section_lines: int = 20) -> Path:
    """Create a MEMORY.md with a large section that should be extracted."""
    lines = ["# Memory", "", "## Quick Reference", "- important fact", ""]
    lines.append("## Architecture")
    for i in range(section_lines):
        lines.append(f"- Architecture detail {i}")
    lines.append("")
    lines.append("## Gotchas")
    for i in range(section_lines):
        lines.append(f"- Gotcha {i}")
    lines.append("")
    (tmp_path / "MEMORY.md").write_text("\n".join(lines) + "\n")
    return tmp_path


def test_extract_oversized_sections_dryrun(tmp_path):
    """Dry-run identifies sections to extract but doesn't write files."""
    _make_large_memory(tmp_path, section_lines=20)
    result = tidy_memory(tmp_path, budget=20, section_threshold=10)

    assert result.under_budget is False
    assert len(result.extractions) >= 1
    assert not any(e.skipped for e in result.extractions)

    # Dry-run: no topic files should exist
    assert not (tmp_path / "architecture.md").exists()
    assert not (tmp_path / "gotchas.md").exists()


def test_extract_oversized_sections_apply(tmp_path):
    """Apply mode writes topic files and rewrites MEMORY.md."""
    _make_large_memory(tmp_path, section_lines=20)
    result = tidy_memory(tmp_path, budget=20, section_threshold=10, apply=True)

    assert result.under_budget is False
    assert len(result.extractions) >= 1

    # Topic files should be created
    for ext in result.extractions:
        if not ext.skipped:
            topic_file = tmp_path / f"{ext.slug}.md"
            assert topic_file.exists(), f"Expected {topic_file} to exist"
            content = topic_file.read_text()
            assert content.startswith(f"# {ext.section}")

    # MEMORY.md should be rewritten
    memory_content = (tmp_path / "MEMORY.md").read_text()
    # Should have topic links
    for ext in result.extractions:
        if not ext.skipped:
            assert f"[{ext.slug}.md]({ext.slug}.md)" in memory_content
    # Quick Reference should still be there
    assert "## Quick Reference" in memory_content


def test_protected_sections_never_extracted(tmp_path):
    """Quick Reference and Topic Files sections are never extracted."""
    lines = ["# Memory", ""]
    lines.append("## Quick Reference")
    for i in range(30):
        lines.append(f"- reference {i}")
    lines.append("")
    lines.append("## Topic Files")
    lines.append("- [existing.md](existing.md) — existing topic")
    lines.append("")
    lines.append("## Small Section")
    lines.append("- just a note")
    lines.append("")
    (tmp_path / "MEMORY.md").write_text("\n".join(lines) + "\n")

    result = tidy_memory(tmp_path, budget=10, section_threshold=5)
    extracted_names = {e.section for e in result.extractions if not e.skipped}
    assert "Quick Reference" not in extracted_names
    assert "Topic Files" not in extracted_names


def test_existing_topic_file_skipped(tmp_path):
    """If a topic file already exists, the section is skipped."""
    _make_large_memory(tmp_path, section_lines=20)
    # Create an existing topic file
    (tmp_path / "architecture.md").write_text("# Existing content\n")

    result = tidy_memory(tmp_path, budget=20, section_threshold=10, apply=True)
    arch_extractions = [e for e in result.extractions if e.section == "Architecture"]
    assert len(arch_extractions) == 1
    assert arch_extractions[0].skipped is True

    # Existing file should not be overwritten
    assert (tmp_path / "architecture.md").read_text() == "# Existing content\n"


def test_topic_files_section_created_when_missing(tmp_path):
    """If no Topic Files section exists, one is created."""
    _make_large_memory(tmp_path, section_lines=20)
    result = tidy_memory(tmp_path, budget=20, section_threshold=10, apply=True)

    memory_content = (tmp_path / "MEMORY.md").read_text()
    assert "## Topic Files" in memory_content


def test_topic_files_section_appended_when_exists(tmp_path):
    """If Topic Files section already exists, new links are appended."""
    lines = ["# Memory", "", "## Quick Reference", "- fact", ""]
    lines.append("## Topic Files")
    lines.append("- [existing.md](existing.md) — Old topic")
    lines.append("")
    lines.append("## Big Section")
    for i in range(25):
        lines.append(f"- detail {i}")
    lines.append("")
    (tmp_path / "MEMORY.md").write_text("\n".join(lines) + "\n")

    result = tidy_memory(tmp_path, budget=15, section_threshold=10, apply=True)

    memory_content = (tmp_path / "MEMORY.md").read_text()
    # Both old and new links should be present
    assert "[existing.md](existing.md)" in memory_content
    assert "[big-section.md](big-section.md)" in memory_content


def test_extracts_largest_first(tmp_path):
    """Sections are extracted largest-first, stopping when under budget."""
    lines = ["# Memory", ""]
    # Medium section (12 lines)
    lines.append("## Medium")
    for i in range(12):
        lines.append(f"- med {i}")
    lines.append("")
    # Large section (25 lines)
    lines.append("## Large")
    for i in range(25):
        lines.append(f"- large {i}")
    lines.append("")
    # Small section (8 lines — below threshold)
    lines.append("## Small")
    for i in range(8):
        lines.append(f"- small {i}")
    lines.append("")
    (tmp_path / "MEMORY.md").write_text("\n".join(lines) + "\n")

    # Budget of 30: extracting Large (26 lines w/heading) should suffice
    result = tidy_memory(tmp_path, budget=30, section_threshold=10)

    # Large should be extracted first
    non_skipped = [e for e in result.extractions if not e.skipped]
    assert len(non_skipped) >= 1
    assert non_skipped[0].section == "Large"


def test_empty_memory_file(tmp_path):
    """Empty MEMORY.md is under budget."""
    (tmp_path / "MEMORY.md").write_text("")
    result = tidy_memory(tmp_path)
    assert result.under_budget is True
    assert result.total_lines == 0


def test_only_protected_sections(tmp_path):
    """File with only protected sections over budget — nothing to extract."""
    lines = ["# Memory", ""]
    lines.append("## Quick Reference")
    for i in range(50):
        lines.append(f"- ref {i}")
    lines.append("")
    lines.append("## Topic Files")
    lines.append("- [a.md](a.md) — topic a")
    lines.append("")
    (tmp_path / "MEMORY.md").write_text("\n".join(lines) + "\n")

    result = tidy_memory(tmp_path, budget=10, section_threshold=5)
    assert result.under_budget is False
    non_skipped = [e for e in result.extractions if not e.skipped]
    assert len(non_skipped) == 0


# --- Stale count detection ---


def test_detect_stale_counts_basic(tmp_path):
    """Detects count patterns in memory files."""
    _write_memory(tmp_path, """\
        # Memory

        ## Quick Reference
        - 48 Interverse plugins, 5 pillars, 3 layers
        - 94 tests across 11 files
    """)
    counts = detect_stale_counts(tmp_path)
    stored_counts = {(c.text, c.stored_count) for c in counts}
    assert ("48 Interverse plugins", 48) in stored_counts
    assert ("94 tests", 94) in stored_counts
    assert ("5 pillars", 5) in stored_counts
    assert ("3 layers", 3) in stored_counts


def test_detect_stale_counts_with_project_dir(tmp_path):
    """Verifiable counts are resolved against the project directory."""
    _write_memory(tmp_path, """\
        # Memory

        ## Quick Reference
        - 3 plugins
    """)
    # Create a project dir with interverse/ containing plugins
    project = tmp_path / "project"
    project.mkdir()
    interverse = project / "interverse"
    interverse.mkdir()
    for name in ["pluginA", "pluginB", "pluginC", "pluginD"]:
        (interverse / name).mkdir()

    counts = detect_stale_counts(tmp_path, project_dir=project)
    plugin_counts = [c for c in counts if "plugin" in c.text.lower()]
    assert len(plugin_counts) == 1
    assert plugin_counts[0].stored_count == 3
    assert plugin_counts[0].actual_count == 4  # 4 dirs but stored says 3


def test_detect_stale_counts_no_counts(tmp_path):
    """No count patterns returns empty list."""
    _write_memory(tmp_path, """\
        # Memory

        ## Gotchas
        - Use command -v not which
    """)
    counts = detect_stale_counts(tmp_path)
    assert counts == []


def test_detect_stale_counts_across_topic_files(tmp_path):
    """Scans topic files in addition to MEMORY.md."""
    _write_memory(tmp_path, """\
        # Memory
        ## Topic Files
        - [arch.md](arch.md) — architecture
    """)
    (tmp_path / "arch.md").write_text("# Arch\n\n- 15 modules in the system\n")

    counts = detect_stale_counts(tmp_path)
    assert len(counts) == 1
    assert counts[0].source_file == "arch.md"
    assert counts[0].stored_count == 15
