"""Integration tests for the full synthesis pipeline."""

from __future__ import annotations

from pathlib import Path

from intermem.journal import PromotionJournal
from intermem.synthesize import SynthesisResult, run_synthesis


def _setup_project(tmp_path: Path, memory_content: str, agents_content: str = "") -> dict[str, Path]:
    """Set up a fake project directory with memory and target docs."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text(memory_content, encoding="utf-8")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    if agents_content:
        (project_dir / "AGENTS.md").write_text(agents_content, encoding="utf-8")

    return {
        "memory_dir": memory_dir,
        "project_dir": project_dir,
        "agents_path": project_dir / "AGENTS.md",
        "claude_path": project_dir / "CLAUDE.md",
        "intermem_dir": tmp_path / ".intermem",
    }


def test_first_run_builds_baseline(tmp_path: Path) -> None:
    """First run records baseline, promotes nothing."""
    env = _setup_project(tmp_path, "## Facts\n- Fact A\n- Fact B\n")
    result = run_synthesis(
        memory_dir=env["memory_dir"],
        target_docs=[env["agents_path"]],
        intermem_dir=env["intermem_dir"],
        auto_approve=True,
    )
    assert isinstance(result, SynthesisResult)
    assert result.baseline_recorded is True
    assert result.promoted_count == 0
    assert (env["intermem_dir"] / "stability.jsonl").exists()


def test_stable_entries_promoted_after_three_runs(tmp_path: Path) -> None:
    """Entries stable across 3 snapshots are promoted on 4th run."""
    env = _setup_project(
        tmp_path,
        "## Facts\n- Stable fact\n",
        "# Project\n\n## Facts\n",
    )
    for _ in range(3):
        run_synthesis(
            memory_dir=env["memory_dir"],
            target_docs=[env["agents_path"]],
            intermem_dir=env["intermem_dir"],
            auto_approve=True,
        )

    result = run_synthesis(
        memory_dir=env["memory_dir"],
        target_docs=[env["agents_path"]],
        intermem_dir=env["intermem_dir"],
        auto_approve=True,
    )
    assert result.promoted_count == 1
    agents_content = env["agents_path"].read_text(encoding="utf-8")
    assert "- Stable fact" in agents_content


def test_volatile_entries_not_promoted(tmp_path: Path) -> None:
    """Entries that change between runs are not promoted."""
    env = _setup_project(tmp_path, "## Facts\n- Version 1\n")

    run_synthesis(
        memory_dir=env["memory_dir"],
        target_docs=[env["agents_path"]],
        intermem_dir=env["intermem_dir"],
        auto_approve=True,
    )

    (env["memory_dir"] / "MEMORY.md").write_text("## Facts\n- Version 2\n", encoding="utf-8")

    result = run_synthesis(
        memory_dir=env["memory_dir"],
        target_docs=[env["agents_path"]],
        intermem_dir=env["intermem_dir"],
        auto_approve=True,
    )
    assert result.promoted_count == 0


def test_exact_duplicates_skipped(tmp_path: Path) -> None:
    """Entries already in target docs are not promoted."""
    env = _setup_project(
        tmp_path,
        "## Facts\n- Already there\n",
        "# Project\n\n## Facts\n- Already there\n",
    )
    for _ in range(4):
        result = run_synthesis(
            memory_dir=env["memory_dir"],
            target_docs=[env["agents_path"]],
            intermem_dir=env["intermem_dir"],
            auto_approve=True,
        )

    assert result.promoted_count == 0
    assert result.duplicates_skipped >= 1


def test_pruning_after_promotion(tmp_path: Path) -> None:
    """Promoted entries are pruned from auto-memory."""
    env = _setup_project(
        tmp_path,
        "## Facts\n- Stable fact\n- Other fact\n",
        "# Project\n\n## Facts\n",
    )
    for _ in range(3):
        run_synthesis(
            memory_dir=env["memory_dir"],
            target_docs=[env["agents_path"]],
            intermem_dir=env["intermem_dir"],
            auto_approve=True,
        )

    result = run_synthesis(
        memory_dir=env["memory_dir"],
        target_docs=[env["agents_path"]],
        intermem_dir=env["intermem_dir"],
        auto_approve=True,
    )

    memory_content = (env["memory_dir"] / "MEMORY.md").read_text(encoding="utf-8")
    assert result.promoted_count > 0
    assert result.pruned_count > 0
    assert "- Stable fact" not in memory_content
    assert "- Other fact" not in memory_content


def test_crash_recovery_prunes_incomplete_committed_entries(tmp_path: Path) -> None:
    """Startup recovery completes committed journal entries left by a crash."""
    env = _setup_project(
        tmp_path,
        "## Facts\n- Recovered fact\n- Keep fact\n",
        "# Project\n\n## Facts\n- Recovered fact <!-- intermem -->\n",
    )
    journal = PromotionJournal(env["intermem_dir"] / "promotion-journal.jsonl")
    journal.record_pending("recover-hash", "AGENTS.md", "Facts", "- Recovered fact")
    journal.mark_committed("recover-hash")

    result = run_synthesis(
        memory_dir=env["memory_dir"],
        target_docs=[env["agents_path"]],
        intermem_dir=env["intermem_dir"],
        auto_approve=True,
    )

    assert result.pruned_count > 0
    memory_content = (env["memory_dir"] / "MEMORY.md").read_text(encoding="utf-8")
    assert "- Recovered fact" not in memory_content
    assert "- Keep fact" in memory_content

    reloaded = PromotionJournal(env["intermem_dir"] / "promotion-journal.jsonl")
    assert reloaded.get_incomplete() == []
