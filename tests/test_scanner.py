"""Tests for auto-memory scanner."""
import textwrap
from pathlib import Path
from intermem.scanner import scan_memory_dir, MemoryEntry


def test_scan_empty_dir(tmp_path):
    """Empty memory dir returns empty list."""
    result = scan_memory_dir(tmp_path)
    assert result.entries == []
    assert result.total_lines == 0


def test_scan_single_file_with_sections(tmp_path):
    """Parse a typical MEMORY.md into section-grouped entries."""
    content = textwrap.dedent("""\
        # Project Memory

        ## Oracle CLI
        - Never use `> file` redirect — use `--write-output <path>`
        - Requires: `DISPLAY=:99 CHROME_PATH=/usr/local/bin/google-chrome-wrapper`

        ## Git Workflow
        - Always commit to main, no feature branches
    """)
    (tmp_path / "MEMORY.md").write_text(content)
    result = scan_memory_dir(tmp_path)
    assert len(result.entries) == 3
    assert result.entries[0].section == "Oracle CLI"
    assert "redirect" in result.entries[0].content
    assert result.entries[2].section == "Git Workflow"


def test_scan_preserves_code_blocks(tmp_path):
    """Code blocks within an entry are kept intact, not split."""
    content = textwrap.dedent("""\
        ## Debugging

        - Use strace to trace credential issues:
          ```bash
          strace -e trace=openat,rename -f git push 2>&1
          ```
    """)
    (tmp_path / "MEMORY.md").write_text(content)
    result = scan_memory_dir(tmp_path)
    assert len(result.entries) == 1
    assert "```bash" in result.entries[0].content
    assert "strace" in result.entries[0].content


def test_scan_multiple_files(tmp_path):
    """Scans all .md files, not just MEMORY.md."""
    (tmp_path / "MEMORY.md").write_text("## Main\n- Fact one\n")
    (tmp_path / "debugging.md").write_text("## Debug\n- Fact two\n")
    result = scan_memory_dir(tmp_path)
    assert len(result.entries) == 2


def test_scan_reports_line_count(tmp_path):
    """Total line count across all files."""
    (tmp_path / "MEMORY.md").write_text("# Title\n\n## Section\n- Line 1\n- Line 2\n")
    result = scan_memory_dir(tmp_path)
    assert result.total_lines == 5


def test_scan_warns_near_cap(tmp_path):
    """Warning flag set when total lines > 150."""
    lines = ["- Fact {}\n".format(i) for i in range(160)]
    (tmp_path / "MEMORY.md").write_text("## Section\n" + "".join(lines))
    result = scan_memory_dir(tmp_path)
    assert result.near_cap is True


def test_scan_entry_has_line_range(tmp_path):
    """Each entry records its source file and line range."""
    content = "## Section\n- First fact\n- Second fact\n"
    (tmp_path / "MEMORY.md").write_text(content)
    result = scan_memory_dir(tmp_path)
    assert result.entries[0].source_file == "MEMORY.md"
    assert result.entries[0].start_line >= 1


def test_scan_classifies_pointer_entries(tmp_path):
    """Entries referencing file paths are classified as pointers."""
    content = textwrap.dedent("""\
        ## Where Knowledge Lives
        - `docs/guides/plugin-troubleshooting.md` — hooks format, cache divergence
        - `hub/clavain/AGENTS.md` — upstream sync, file mapping

        ## Cross-Cutting Lessons
        - Never use `> file` redirect — use `--write-output <path>`
    """)
    (tmp_path / "MEMORY.md").write_text(content)
    result = scan_memory_dir(tmp_path)
    assert len(result.entries) == 3
    assert result.entries[0].is_pointer is True
    assert result.entries[1].is_pointer is True
    assert result.entries[2].is_pointer is False


def test_scan_non_path_backtick_not_pointer(tmp_path):
    """Entries with backtick code that aren't file paths are not pointers."""
    content = "## Tips\n- Use `pytest -v` for verbose output\n"
    (tmp_path / "MEMORY.md").write_text(content)
    result = scan_memory_dir(tmp_path)
    assert result.entries[0].is_pointer is False
