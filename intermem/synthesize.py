"""Synthesis pipeline orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from intermem.dedup import check_duplicates
from intermem.journal import PromotionJournal
from intermem.promoter import promote_entries
from intermem.pruner import prune_promoted
from intermem.scanner import MemoryEntry, scan_memory_dir
from intermem.stability import StabilityStore, record_snapshot, score_entries
from intermem._util import hash_entry, normalize_content


@dataclass
class SynthesisResult:
    """Summary of a full synthesis run."""

    total_entries: int
    stable_entries: int
    duplicates_skipped: int
    candidates_count: int
    promoted_count: int
    pruned_count: int
    baseline_recorded: bool
    new_line_count: int
    validated_count: int = 0
    stale_filtered: int = 0
    candidates_deferred: int = 0


def _contains_promoted_content(target_path: Path, content: str) -> bool:
    if not target_path.exists():
        return False
    text = target_path.read_text(encoding="utf-8")
    return content in text


def _recover_incomplete_journal(
    journal: PromotionJournal,
    memory_dir: Path,
    target_docs: list[Path],
    dry_run: bool,
) -> int:
    """Recover pending/committed journal entries left by an interrupted run."""
    incomplete = journal.get_incomplete()
    if not incomplete:
        return 0

    target_by_name = {path.name: path for path in target_docs}
    if not dry_run:
        for item in incomplete:
            if item.status != "pending":
                continue
            target_path = target_by_name.get(item.target_file)
            if target_path is None:
                continue
            if _contains_promoted_content(target_path, item.content):
                journal.mark_committed(item.entry_hash)

    committed = [item for item in journal.get_incomplete() if item.status == "committed"]
    if not committed:
        return 0

    committed_by_content: dict[str, list[str]] = {}
    for item in committed:
        key = normalize_content(item.content)
        committed_by_content.setdefault(key, []).append(item.entry_hash)

    scanned = scan_memory_dir(memory_dir)
    entries_to_prune: list[MemoryEntry] = []
    present_hashes: set[str] = set()
    for entry in scanned.entries:
        key = normalize_content(entry.content)
        hashes = committed_by_content.get(key)
        if not hashes:
            continue
        entries_to_prune.append(entry)
        present_hashes.update(hashes)

    if not dry_run:
        for item in committed:
            if item.entry_hash in present_hashes:
                continue
            journal.mark_pruned(item.entry_hash)

    if not entries_to_prune:
        return 0

    prune_result = prune_promoted(
        entries=entries_to_prune,
        memory_dir=memory_dir,
        journal=journal,
        dry_run=dry_run,
    )
    return prune_result.lines_removed


def run_synthesis(
    memory_dir: Path,
    target_docs: list[Path],
    intermem_dir: Path,
    auto_approve: bool = False,
    dry_run: bool = False,
    validate: bool = False,
    project_root: Path | None = None,
    max_candidates: int = 0,
) -> SynthesisResult:
    """Run the full synthesis pipeline."""
    intermem_dir.mkdir(parents=True, exist_ok=True)
    journal = PromotionJournal(intermem_dir / "promotion-journal.jsonl")

    recovered_pruned = _recover_incomplete_journal(
        journal=journal,
        memory_dir=memory_dir,
        target_docs=target_docs,
        dry_run=dry_run,
    )

    scan = scan_memory_dir(memory_dir)
    if not scan.entries:
        return SynthesisResult(
            total_entries=0,
            stable_entries=0,
            duplicates_skipped=0,
            candidates_count=0,
            promoted_count=0,
            pruned_count=recovered_pruned,
            baseline_recorded=False,
            new_line_count=scan.total_lines,
        )

    stability_store = StabilityStore(intermem_dir / "stability.jsonl")
    is_first_run = len(stability_store.snapshots) == 0
    if is_first_run:
        record_snapshot(stability_store, scan.entries)
        return SynthesisResult(
            total_entries=len(scan.entries),
            stable_entries=0,
            duplicates_skipped=0,
            candidates_count=0,
            promoted_count=0,
            pruned_count=recovered_pruned,
            baseline_recorded=True,
            new_line_count=scan.total_lines,
        )

    # Record current snapshot first, then score including it.
    # First run is baseline (returns above). Stability requires 3+ appearances,
    # so: run 1 = baseline, run 2 = 2 snapshots, run 3 = 3 snapshots = stable.
    record_snapshot(stability_store, scan.entries)
    scores = score_entries(stability_store, scan.entries)

    stable = [score for score in scores if score.score == "stable"]
    # Build hash→snapshot_count map for candidate ranking.
    snapshot_counts = {hash_entry(s.entry): s.snapshot_count for s in stable}
    if not stable:
        return SynthesisResult(
            total_entries=len(scan.entries),
            stable_entries=0,
            duplicates_skipped=0,
            candidates_count=0,
            promoted_count=0,
            pruned_count=recovered_pruned,
            baseline_recorded=False,
            new_line_count=scan.total_lines,
        )

    # Exclude pointer entries (file path references, not standalone facts).
    stable_entries = [score.entry for score in stable if not score.entry.is_pointer]
    if not stable_entries:
        return SynthesisResult(
            total_entries=len(scan.entries),
            stable_entries=len(stable),
            duplicates_skipped=0,
            candidates_count=0,
            promoted_count=0,
            pruned_count=recovered_pruned,
            baseline_recorded=False,
            new_line_count=scan.total_lines,
        )

    # Validate citations and filter stale entries.
    validated_count = 0
    stale_filtered = 0
    if validate and project_root is not None:
        from intermem.metadata import MetadataStore
        from intermem.validator import validate_and_filter_entries

        metadata_store = MetadataStore(intermem_dir / "metadata.db")
        try:
            val_result = validate_and_filter_entries(
                stable_entries, metadata_store, project_root
            )
            stable_entries = val_result.validated_entries
            validated_count = val_result.validated_count
            stale_filtered = val_result.stale_count
        finally:
            metadata_store.close()

    dedup_results = check_duplicates(stable_entries, target_docs)
    exact_dupes = [item for item in dedup_results if item.status == "exact_duplicate"]
    candidates = [
        item.entry
        for item in dedup_results
        if item.status in {"novel", "fuzzy_duplicate"}
    ]

    # Cap candidates to avoid cold-start flood on first promotion run.
    # Rank by snapshot_count descending (longest-surviving entries first).
    candidates_deferred = 0
    if max_candidates > 0 and len(candidates) > max_candidates:
        candidates.sort(
            key=lambda e: snapshot_counts.get(hash_entry(e), 0), reverse=True
        )
        candidates_deferred = len(candidates) - max_candidates
        candidates = candidates[:max_candidates]

    if not candidates:
        return SynthesisResult(
            total_entries=len(scan.entries),
            stable_entries=len(stable),
            duplicates_skipped=len(exact_dupes),
            candidates_count=0,
            promoted_count=0,
            pruned_count=recovered_pruned,
            baseline_recorded=False,
            new_line_count=scan.total_lines,
            validated_count=validated_count,
            stale_filtered=stale_filtered,
            candidates_deferred=candidates_deferred,
        )

    approved = candidates if auto_approve else []
    if not approved or dry_run:
        return SynthesisResult(
            total_entries=len(scan.entries),
            stable_entries=len(stable),
            duplicates_skipped=len(exact_dupes),
            candidates_count=len(candidates),
            promoted_count=0,
            pruned_count=recovered_pruned,
            baseline_recorded=False,
            new_line_count=scan.total_lines,
            validated_count=validated_count,
            stale_filtered=stale_filtered,
            candidates_deferred=candidates_deferred,
        )

    target = target_docs[0] if target_docs else None
    if target is None:
        return SynthesisResult(
            total_entries=len(scan.entries),
            stable_entries=len(stable),
            duplicates_skipped=len(exact_dupes),
            candidates_count=len(candidates),
            promoted_count=0,
            pruned_count=recovered_pruned,
            baseline_recorded=False,
            new_line_count=scan.total_lines,
            validated_count=validated_count,
            stale_filtered=stale_filtered,
            candidates_deferred=candidates_deferred,
        )

    promotion_result = promote_entries(approved, target, journal)
    prune_result = prune_promoted(approved, memory_dir, journal, dry_run=dry_run)

    return SynthesisResult(
        total_entries=len(scan.entries),
        stable_entries=len(stable),
        duplicates_skipped=len(exact_dupes),
        candidates_count=len(candidates),
        promoted_count=promotion_result.promoted_count,
        pruned_count=recovered_pruned + prune_result.lines_removed,
        baseline_recorded=False,
        new_line_count=prune_result.new_total_lines,
        validated_count=validated_count,
        stale_filtered=stale_filtered,
        candidates_deferred=candidates_deferred,
    )
