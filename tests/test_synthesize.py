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
    """Entries stable across 3 runs are promoted on 3rd run."""
    env = _setup_project(
        tmp_path,
        "## Facts\n- Stable fact\n",
        "# Project\n\n## Facts\n",
    )
    # Run 1: baseline. Run 2: 2 snapshots (recent). Run 3: 3 snapshots (stable + promote).
    for _ in range(2):
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
    for _ in range(3):
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
    for _ in range(2):
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


def test_pointer_entries_excluded_from_promotion(tmp_path: Path) -> None:
    """Pointer entries (file path references) are never promoted."""
    env = _setup_project(
        tmp_path,
        "## Where Knowledge Lives\n- `docs/guides/foo.md` — hooks format\n\n## Facts\n- Real fact\n",
        "# Project\n\n## Facts\n",
    )
    for _ in range(2):
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
    agents_content = env["agents_path"].read_text(encoding="utf-8")
    # The pointer should not be promoted, but the real fact should
    assert "docs/guides/foo.md" not in agents_content
    assert "- Real fact" in agents_content
    assert result.promoted_count == 1


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


# -- Phase 1 validation integration tests --


def test_synthesis_with_validation_filters_stale(tmp_path: Path) -> None:
    """End-to-end: entry with broken citation not promoted when validate=True."""
    env = _setup_project(
        tmp_path,
        "## Facts\n- Edit `src/deleted.py` and `lib/gone.py` for changes\n",
        "# Project\n\n## Facts\n",
    )
    # Run 3 times to reach stability.
    for _ in range(2):
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
        validate=True,
        project_root=env["project_dir"],
    )
    # Stale entry should be filtered — not promoted.
    assert result.validated_count > 0
    assert result.stale_filtered > 0
    assert result.promoted_count == 0


def test_synthesis_validate_false_behaves_like_phase05(tmp_path: Path) -> None:
    """Synthesis with validate=False passes stale entries through (Phase 0.5 behavior)."""
    env = _setup_project(
        tmp_path,
        "## Facts\n- Edit `src/deleted.py` and `lib/gone.py` for changes\n",
        "# Project\n\n## Facts\n",
    )
    for _ in range(2):
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
        validate=False,
    )
    # Without validation, stale entry passes through and gets promoted.
    assert result.validated_count == 0
    assert result.stale_filtered == 0
    assert result.promoted_count == 1


def test_synthesis_result_includes_validation_fields(tmp_path: Path) -> None:
    """SynthesisResult always includes validated_count and stale_filtered."""
    env = _setup_project(tmp_path, "## Facts\n- Fact A\n")
    result = run_synthesis(
        memory_dir=env["memory_dir"],
        target_docs=[env["agents_path"]],
        intermem_dir=env["intermem_dir"],
    )
    assert hasattr(result, "validated_count")
    assert hasattr(result, "stale_filtered")
    assert result.validated_count == 0
    assert result.stale_filtered == 0


# -- Cold-start flood capping tests --


def test_max_candidates_caps_promotion_count(tmp_path: Path) -> None:
    """When max_candidates is set, only that many entries are promoted."""
    env = _setup_project(
        tmp_path,
        "## Facts\n- Fact A\n- Fact B\n- Fact C\n- Fact D\n- Fact E\n",
        "# Project\n\n## Facts\n",
    )
    for _ in range(2):
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
        max_candidates=2,
    )
    assert result.promoted_count == 2
    assert result.candidates_deferred == 3
    assert result.candidates_count == 2


def test_max_candidates_zero_means_unlimited(tmp_path: Path) -> None:
    """max_candidates=0 promotes all candidates (default behavior)."""
    env = _setup_project(
        tmp_path,
        "## Facts\n- Fact A\n- Fact B\n- Fact C\n",
        "# Project\n\n## Facts\n",
    )
    for _ in range(2):
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
        max_candidates=0,
    )
    assert result.promoted_count == 3
    assert result.candidates_deferred == 0


def test_max_candidates_no_cap_when_under_limit(tmp_path: Path) -> None:
    """When candidates <= max_candidates, no deferral happens."""
    env = _setup_project(
        tmp_path,
        "## Facts\n- Fact A\n- Fact B\n",
        "# Project\n\n## Facts\n",
    )
    for _ in range(2):
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
        max_candidates=5,
    )
    assert result.promoted_count == 2
    assert result.candidates_deferred == 0


def test_max_candidates_ranks_by_snapshot_count(tmp_path: Path) -> None:
    """Capping prefers entries with higher snapshot counts."""
    env = _setup_project(
        tmp_path,
        "## Facts\n- Old fact\n",
        "# Project\n\n## Facts\n",
    )
    # Run 3 times with "Old fact" to give it 3 snapshots.
    for _ in range(2):
        run_synthesis(
            memory_dir=env["memory_dir"],
            target_docs=[env["agents_path"]],
            intermem_dir=env["intermem_dir"],
        )

    # Now add a new fact; it'll have fewer snapshots.
    (env["memory_dir"] / "MEMORY.md").write_text(
        "## Facts\n- Old fact\n- New fact\n", encoding="utf-8"
    )
    # Run once more so "New fact" gets snapshot 1 (baseline already past).
    run_synthesis(
        memory_dir=env["memory_dir"],
        target_docs=[env["agents_path"]],
        intermem_dir=env["intermem_dir"],
    )
    # And again so "New fact" gets snapshot 2.
    run_synthesis(
        memory_dir=env["memory_dir"],
        target_docs=[env["agents_path"]],
        intermem_dir=env["intermem_dir"],
    )
    # And a third time for "New fact" to reach 3 snapshots = stable.
    # "Old fact" now has 5 snapshots.
    result = run_synthesis(
        memory_dir=env["memory_dir"],
        target_docs=[env["agents_path"]],
        intermem_dir=env["intermem_dir"],
        auto_approve=True,
        max_candidates=1,
    )
    # Only 1 promoted — should be "Old fact" (higher snapshot count).
    assert result.promoted_count == 1
    assert result.candidates_deferred == 1
    agents_content = env["agents_path"].read_text(encoding="utf-8")
    assert "Old fact" in agents_content
    assert "New fact" not in agents_content


def test_max_candidates_deferred_promoted_on_next_run(tmp_path: Path) -> None:
    """Deferred candidates are promoted on subsequent runs."""
    env = _setup_project(
        tmp_path,
        "## Facts\n- Fact A\n- Fact B\n- Fact C\n",
        "# Project\n\n## Facts\n",
    )
    for _ in range(2):
        run_synthesis(
            memory_dir=env["memory_dir"],
            target_docs=[env["agents_path"]],
            intermem_dir=env["intermem_dir"],
            auto_approve=True,
        )

    # First capped run: promote 1, defer 2.
    r1 = run_synthesis(
        memory_dir=env["memory_dir"],
        target_docs=[env["agents_path"]],
        intermem_dir=env["intermem_dir"],
        auto_approve=True,
        max_candidates=1,
    )
    assert r1.promoted_count == 1
    assert r1.candidates_deferred == 2

    # Second capped run: promote 1 more, defer 1.
    r2 = run_synthesis(
        memory_dir=env["memory_dir"],
        target_docs=[env["agents_path"]],
        intermem_dir=env["intermem_dir"],
        auto_approve=True,
        max_candidates=1,
    )
    assert r2.promoted_count == 1
    assert r2.candidates_deferred == 1

    # Third run: promote last one.
    r3 = run_synthesis(
        memory_dir=env["memory_dir"],
        target_docs=[env["agents_path"]],
        intermem_dir=env["intermem_dir"],
        auto_approve=True,
        max_candidates=1,
    )
    assert r3.promoted_count == 1
    assert r3.candidates_deferred == 0
