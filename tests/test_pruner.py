"""Tests for auto-memory pruner — removes promoted entries from source files."""
from pathlib import Path

from intermem.journal import PromotionJournal
from intermem.pruner import PruneResult, prune_promoted
from intermem.scanner import MemoryEntry, scan_memory_dir


def _setup_memory(tmp_path: Path, content: str) -> Path:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text(content, encoding="utf-8")
    return memory_dir


def test_prune_removes_promoted_entry(tmp_path):
    """Promoted entry is removed from auto-memory."""
    content = "## Section\n- Fact A\n- Fact B\n- Fact C\n"
    memory_dir = _setup_memory(tmp_path, content)

    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    journal.record_pending("fakehash", "AGENTS.md", "Section", "- Fact B")
    journal.mark_committed("fakehash")

    entry = MemoryEntry(
        content="- Fact B",
        section="Section",
        source_file="MEMORY.md",
        start_line=3,
        end_line=3,
    )
    result = prune_promoted([entry], memory_dir, journal)

    remaining = (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "- Fact A" in remaining
    assert "- Fact B" not in remaining
    assert "- Fact C" in remaining
    assert result.lines_removed > 0


def test_prune_creates_backup(tmp_path):
    """Backup .bak file is created before pruning."""
    content = "## Section\n- Fact A\n- Fact B\n"
    memory_dir = _setup_memory(tmp_path, content)

    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    journal.record_pending("h", "AGENTS.md", "Section", "- Fact B")
    journal.mark_committed("h")

    entry = MemoryEntry(
        content="- Fact B",
        section="Section",
        source_file="MEMORY.md",
        start_line=3,
        end_line=3,
    )
    prune_promoted([entry], memory_dir, journal)

    backup_path = memory_dir / "MEMORY.md.bak"
    assert backup_path.exists()
    backup = backup_path.read_text(encoding="utf-8")
    assert "- Fact B" in backup


def test_prune_dry_run(tmp_path):
    """Dry run reports what would be removed without modifying files."""
    content = "## Section\n- Fact A\n- Fact B\n"
    memory_dir = _setup_memory(tmp_path, content)

    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    journal.record_pending("h", "AGENTS.md", "Section", "- Fact B")
    journal.mark_committed("h")

    entry = MemoryEntry(
        content="- Fact B",
        section="Section",
        source_file="MEMORY.md",
        start_line=3,
        end_line=3,
    )
    result = prune_promoted([entry], memory_dir, journal, dry_run=True)

    remaining = (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "- Fact B" in remaining
    assert result.lines_removed > 0
    assert result.dry_run is True


def test_prune_cleans_orphaned_headers(tmp_path):
    """Section header is removed if all its entries are pruned."""
    content = "## Main\n- Keep this\n\n## Orphan\n- Remove me\n"
    memory_dir = _setup_memory(tmp_path, content)

    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    journal.record_pending("h", "AGENTS.md", "Orphan", "- Remove me")
    journal.mark_committed("h")

    entry = MemoryEntry(
        content="- Remove me",
        section="Orphan",
        source_file="MEMORY.md",
        start_line=5,
        end_line=5,
    )
    prune_promoted([entry], memory_dir, journal)

    remaining = (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "## Orphan" not in remaining
    assert "## Main" in remaining
    assert "- Keep this" in remaining


def test_prune_marks_journal_pruned(tmp_path):
    """After successful prune, journal entry is marked pruned."""
    content = "## Section\n- Fact A\n"
    memory_dir = _setup_memory(tmp_path, content)

    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    journal.record_pending("h", "AGENTS.md", "Section", "- Fact A")
    journal.mark_committed("h")

    entry = MemoryEntry(
        content="- Fact A",
        section="Section",
        source_file="MEMORY.md",
        start_line=2,
        end_line=2,
    )
    prune_promoted([entry], memory_dir, journal)

    incomplete = journal.get_incomplete()
    assert len(incomplete) == 0


def test_prune_reports_new_line_count(tmp_path):
    """Result includes new total line count after pruning."""
    content = "## Section\n- Fact A\n- Fact B\n- Fact C\n"
    memory_dir = _setup_memory(tmp_path, content)

    journal = PromotionJournal(tmp_path / ".intermem" / "promotion-journal.jsonl")
    journal.record_pending("h", "AGENTS.md", "Section", "- Fact B")
    journal.mark_committed("h")

    entry = MemoryEntry(
        content="- Fact B",
        section="Section",
        source_file="MEMORY.md",
        start_line=3,
        end_line=3,
    )
    result = prune_promoted([entry], memory_dir, journal)

    assert isinstance(result, PruneResult)
    assert result.new_total_lines < 4
    assert result.lines_removed >= 1
    assert result.new_total_lines == scan_memory_dir(memory_dir).total_lines
