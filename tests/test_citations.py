"""Tests for citation extraction, resolution, validation, and confidence scoring."""
from __future__ import annotations

import os

import pytest

from intermem.citations import (
    Citation,
    CheckResult,
    compute_confidence,
    extract_citations,
    resolve_citation,
    validate_citation,
)
from intermem.scanner import MemoryEntry


def _entry(content: str) -> MemoryEntry:
    """Helper to create a MemoryEntry with minimal fields."""
    return MemoryEntry(
        content=content,
        section="Test",
        source_file="test.md",
        start_line=1,
        end_line=1,
    )


# -- Extraction Tests --


class TestExtractCitations:
    def test_backtick_file_path(self):
        entry = _entry("- Use `src/store/db.ts` for database access")
        citations = extract_citations(entry)
        assert len(citations) == 1
        assert citations[0].type == "file_path"
        assert citations[0].value == "src/store/db.ts"

    def test_backtick_module_reference(self):
        entry = _entry("- The `plugins/interkasten` module handles sync")
        citations = extract_citations(entry)
        assert len(citations) == 1
        assert citations[0].type == "module"
        assert citations[0].value == "plugins/interkasten"

    def test_markdown_link_path(self):
        entry = _entry("- See [guide](docs/guides/setup.md) for details")
        citations = extract_citations(entry)
        assert len(citations) == 1
        assert citations[0].type == "file_path"
        assert citations[0].value == "docs/guides/setup.md"

    def test_absolute_path(self):
        entry = _entry("- Config lives at /root/projects/Interverse/CLAUDE.md")
        citations = extract_citations(entry)
        assert len(citations) == 1
        assert citations[0].type == "file_path"
        assert citations[0].value == "/root/projects/Interverse/CLAUDE.md"

    def test_multiple_citations(self):
        entry = _entry(
            "- Edit `src/main.py` and `src/config.yaml` for settings"
        )
        citations = extract_citations(entry)
        assert len(citations) == 2
        values = {c.value for c in citations}
        assert "src/main.py" in values
        assert "src/config.yaml" in values

    def test_deduplicates_same_path(self):
        entry = _entry("- Use `src/main.py` because `src/main.py` is the entry point")
        citations = extract_citations(entry)
        assert len(citations) == 1

    def test_ignores_urls(self):
        entry = _entry("- Report at `https://github.com/user/repo/issues`")
        citations = extract_citations(entry)
        assert len(citations) == 0

    def test_ignores_protocol_urls(self):
        entry = _entry("- Connect via `ssh://user@host/path`")
        citations = extract_citations(entry)
        assert len(citations) == 0

    def test_ignores_cli_flags(self):
        entry = _entry("- Use `--write-output /path/to/file`")
        citations = extract_citations(entry)
        # Should not extract --write-output as a path.
        # May extract /path/to/file as an absolute path.
        for c in citations:
            assert not c.value.startswith("--")

    def test_ignores_env_vars(self):
        entry = _entry("- Set `DISPLAY=:99` and `VAR=value/other`")
        citations = extract_citations(entry)
        for c in citations:
            assert "=" not in c.value.split("/")[0]

    def test_ignores_shell_variables(self):
        entry = _entry("- Use `$HOME/config/file`")
        citations = extract_citations(entry)
        assert len(citations) == 0

    def test_no_citations_in_plain_text(self):
        entry = _entry("- Always use WAL mode for SQLite databases")
        citations = extract_citations(entry)
        assert len(citations) == 0

    def test_classifies_file_by_extension(self):
        entry = _entry("- Read `intermem/metadata.py`")
        citations = extract_citations(entry)
        assert citations[0].type == "file_path"

    def test_classifies_directory_as_module(self):
        entry = _entry("- The `os/clavain` module orchestrates")
        citations = extract_citations(entry)
        assert citations[0].type == "module"

    def test_strips_trailing_punctuation(self):
        entry = _entry("- Check `docs/guides/setup.md`.")
        citations = extract_citations(entry)
        assert citations[0].value == "docs/guides/setup.md"


# -- Resolution Tests --


class TestResolveCitation:
    def test_resolve_relative_path(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").touch()
        citation = Citation(type="file_path", value="src/main.py", raw_match="`src/main.py`")
        resolved = resolve_citation(citation, tmp_path)
        assert resolved is not None
        assert resolved == (tmp_path / "src" / "main.py").resolve()

    def test_reject_path_traversal(self, tmp_path):
        citation = Citation(
            type="file_path",
            value="../../etc/passwd",
            raw_match="`../../etc/passwd`",
        )
        resolved = resolve_citation(citation, tmp_path)
        assert resolved is None

    def test_reject_absolute_escape(self, tmp_path):
        citation = Citation(
            type="file_path",
            value="/etc/passwd",
            raw_match="`/etc/passwd`",
        )
        resolved = resolve_citation(citation, tmp_path)
        assert resolved is None

    def test_resolve_nonexistent_file(self, tmp_path):
        """Resolution succeeds (path is within boundary) even if file doesn't exist."""
        citation = Citation(
            type="file_path",
            value="does/not/exist.py",
            raw_match="`does/not/exist.py`",
        )
        resolved = resolve_citation(citation, tmp_path)
        assert resolved is not None  # Resolves, but file won't exist

    def test_symlink_escape(self, tmp_path):
        """Symlink pointing outside project root should be rejected."""
        target = tmp_path / "project"
        target.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").touch()

        # Create symlink inside project pointing to outside.
        (target / "link").symlink_to(outside)

        citation = Citation(
            type="file_path",
            value="link/secret.txt",
            raw_match="`link/secret.txt`",
        )
        resolved = resolve_citation(citation, target)
        # resolve() follows symlink → /tmp/.../outside/secret.txt
        # is_relative_to(target) → False
        assert resolved is None


# -- Validation Tests --


class TestValidateCitation:
    def test_valid_file(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").touch()
        citation = Citation(type="file_path", value="src/main.py", raw_match="")
        result = validate_citation(citation, tmp_path)
        assert result.status == "valid"
        assert result.confidence_delta == 0.3

    def test_broken_file(self, tmp_path):
        citation = Citation(type="file_path", value="src/missing.py", raw_match="")
        result = validate_citation(citation, tmp_path)
        assert result.status == "broken"
        assert result.confidence_delta == -0.4
        assert "not found" in result.detail.lower()

    def test_valid_module_directory(self, tmp_path):
        (tmp_path / "plugins" / "intermem").mkdir(parents=True)
        citation = Citation(type="module", value="plugins/intermem", raw_match="")
        result = validate_citation(citation, tmp_path)
        assert result.status == "valid"
        assert result.confidence_delta == 0.3

    def test_broken_module(self, tmp_path):
        citation = Citation(type="module", value="plugins/nonexistent", raw_match="")
        result = validate_citation(citation, tmp_path)
        assert result.status == "broken"
        assert result.confidence_delta == -0.4

    def test_unresolvable_returns_unchecked(self, tmp_path):
        citation = Citation(type="file_path", value="../../etc/passwd", raw_match="")
        result = validate_citation(citation, tmp_path)
        assert result.status == "unchecked"
        assert result.confidence_delta == 0.0


# -- Confidence Tests --


class TestComputeConfidence:
    def test_no_citations_returns_base(self):
        assert compute_confidence(0.5, [], snapshot_count=1) == 0.5

    def test_all_valid(self):
        checks = [
            CheckResult(status="valid", confidence_delta=0.3, detail=""),
            CheckResult(status="valid", confidence_delta=0.3, detail=""),
        ]
        result = compute_confidence(0.5, checks, snapshot_count=1)
        assert result == pytest.approx(1.0)  # 0.5 + 0.3 + 0.3 = 1.1, clamped to 1.0

    def test_one_broken(self):
        checks = [
            CheckResult(status="valid", confidence_delta=0.3, detail=""),
            CheckResult(status="broken", confidence_delta=-0.4, detail=""),
        ]
        result = compute_confidence(0.5, checks, snapshot_count=1)
        assert result == pytest.approx(0.4)  # 0.5 + 0.3 - 0.4

    def test_all_broken(self):
        checks = [
            CheckResult(status="broken", confidence_delta=-0.4, detail=""),
            CheckResult(status="broken", confidence_delta=-0.4, detail=""),
        ]
        result = compute_confidence(0.5, checks, snapshot_count=1)
        assert result == pytest.approx(0.0)  # 0.5 - 0.4 - 0.4 = -0.3, clamped to 0.0

    def test_snapshot_bonus(self):
        result = compute_confidence(0.5, [], snapshot_count=5)
        assert result == pytest.approx(0.7)  # 0.5 + 0.2

    def test_snapshot_bonus_not_applied_below_threshold(self):
        result = compute_confidence(0.5, [], snapshot_count=4)
        assert result == pytest.approx(0.5)

    def test_clamp_high(self):
        checks = [CheckResult(status="valid", confidence_delta=0.3, detail="")] * 10
        result = compute_confidence(0.5, checks, snapshot_count=10)
        assert result == 1.0

    def test_clamp_low(self):
        checks = [CheckResult(status="broken", confidence_delta=-0.4, detail="")] * 10
        result = compute_confidence(0.5, checks, snapshot_count=0)
        assert result == 0.0

    def test_unchecked_has_no_effect(self):
        checks = [
            CheckResult(status="unchecked", confidence_delta=0.0, detail=""),
        ]
        result = compute_confidence(0.5, checks, snapshot_count=1)
        assert result == pytest.approx(0.5)
