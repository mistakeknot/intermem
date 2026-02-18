"""Tests for CLI subcommands — sweep and query."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from intermem.metadata import MetadataStore


@pytest.fixture
def project(tmp_path):
    """Set up a minimal project directory with metadata.db."""
    intermem_dir = tmp_path / ".intermem"
    intermem_dir.mkdir()
    store = MetadataStore(intermem_dir / "metadata.db")
    store.upsert_entry("h1", "SQLite WAL mode", "Database", "MEMORY.md")
    store.upsert_entry("h2", "Git workflow", "Git", "MEMORY.md")
    store.upsert_entry("h3", "Demoted fact", "Database", "MEMORY.md")
    store.mark_demoted("h3")
    store.conn.commit()
    store.close()
    return tmp_path


def _run_cli(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "intermem", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=30,
    )


class TestSweepCommand:
    def test_sweep_runs(self, project):
        result = _run_cli("sweep", "--project-dir", str(project), "--json")
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "entries_swept" in data

    def test_sweep_empty_db(self, tmp_path):
        """Empty DB reports no entries to sweep."""
        intermem_dir = tmp_path / ".intermem"
        intermem_dir.mkdir()
        store = MetadataStore(intermem_dir / "metadata.db")
        store.close()

        result = _run_cli("sweep", "--project-dir", str(tmp_path))
        assert result.returncode == 0
        assert "No entries to sweep" in result.stdout


class TestQueryCommand:
    def test_query_search(self, project):
        result = _run_cli("query", "--search", "SQLite", "--project-dir", str(project), "--json")
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert len(data) == 1  # h1 matches, h3 is demoted but also matches
        # h1 has "SQLite WAL mode", h3 is demoted with "Demoted fact"
        hashes = {r["entry_hash"] for r in data}
        assert "h1" in hashes

    def test_query_topics(self, project):
        result = _run_cli("query", "--topics", "--project-dir", str(project), "--json")
        assert result.returncode == 0
        data = json.loads(result.stdout)
        sections = {r["section"] for r in data}
        assert "Database" in sections
        assert "Git" in sections

    def test_query_demoted(self, project):
        result = _run_cli("query", "--demoted", "--project-dir", str(project), "--json")
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert len(data) == 1
        assert data[0]["entry_hash"] == "h3"

    def test_query_missing_db(self, tmp_path):
        """Missing metadata.db exits with code 2."""
        result = _run_cli("query", "--search", "test", "--project-dir", str(tmp_path))
        assert result.returncode == 2
        assert "metadata.db not found" in result.stderr

    def test_query_no_flag(self, project):
        """Missing query flag exits with code 1."""
        result = _run_cli("query", "--project-dir", str(project))
        assert result.returncode == 1
        assert "--search" in result.stderr


class TestBackwardCompat:
    def test_no_subcommand_validate_only(self, project):
        """--validate-only works without a subcommand."""
        (project / "AGENTS.md").write_text("# Agents\n\n## Testing\n- Fact\n")
        result = _run_cli("--validate-only", "--project-dir", str(project))
        assert result.returncode == 0
        assert "Validated" in result.stdout

    def test_no_subcommand_with_json(self, project):
        """--validate-only --json works without a subcommand."""
        (project / "AGENTS.md").write_text("# Agents\n\n## Testing\n- Fact\n")
        result = _run_cli("--validate-only", "--json", "--project-dir", str(project))
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "entries_checked" in data
