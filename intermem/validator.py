"""Validation pipeline — validates citations and filters stale entries."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from intermem._util import hash_entry
from intermem.citations import (
    CheckResult,
    Citation,
    compute_confidence,
    extract_citations,
    resolve_citation,
    validate_citation,
)
from intermem.journal import PromotionJournal
from intermem.metadata import STALE_THRESHOLD, MetadataStore
from intermem.scanner import MemoryEntry

DEMOTION_STREAK_THRESHOLD = 2


@dataclass
class ValidationResult:
    """Result of validating a set of entries."""

    validated_entries: list[MemoryEntry] = field(default_factory=list)
    stale_filtered: list[MemoryEntry] = field(default_factory=list)
    validated_count: int = 0
    stale_count: int = 0


@dataclass
class PromotedEntryReport:
    """Report for a single promoted entry's validation status."""

    content_preview: str
    section: str
    citations_found: int
    citations_valid: int
    citations_broken: int
    confidence: float
    broken_details: list[str] = field(default_factory=list)


@dataclass
class ValidatePromotedResult:
    """Result of validating already-promoted entries."""

    entries_checked: int
    entries_healthy: int
    entries_stale: int
    reports: list[PromotedEntryReport] = field(default_factory=list)


def validate_and_filter_entries(
    entries: list[MemoryEntry],
    metadata_store: MetadataStore,
    project_root: Path,
) -> ValidationResult:
    """Validate citations for entries, update metadata, filter stale entries.

    Wraps the full operation in a transaction for crash safety.
    """
    if not entries:
        return ValidationResult()

    metadata_store.begin_transaction()
    try:
        for entry in entries:
            entry_hash = hash_entry(entry)
            preview = entry.content[:80].replace("\n", " ")
            metadata_store.upsert_entry(
                entry_hash, preview, entry.section, entry.source_file
            )

            citations = extract_citations(entry)
            checks: list[CheckResult] = []
            for citation in citations:
                resolved = resolve_citation(citation, project_root)
                if resolved is not None:
                    try:
                        resolved_str = str(resolved.relative_to(project_root.resolve()))
                    except ValueError:
                        resolved_str = str(resolved)
                else:
                    resolved_str = None
                metadata_store.record_citation(
                    entry_hash, citation.type, citation.value, resolved_str
                )
                result = validate_citation(citation, project_root)
                metadata_store.record_check(
                    entry_hash,
                    citation.value,
                    citation.type,
                    result.status,
                    result.detail or None,
                )
                checks.append(result)

            # Get snapshot_count and status from the just-upserted row.
            row = metadata_store.get_entry(entry_hash)
            snapshot_count = row["snapshot_count"] if row else 1
            was_demoted = row is not None and row["status"] == "demoted"

            confidence = compute_confidence(0.5, checks, snapshot_count)

            if was_demoted and confidence >= STALE_THRESHOLD:
                # Re-promote: entry reappeared with good confidence.
                metadata_store.reactivate_entry(entry_hash)
            metadata_store.update_confidence(entry_hash, confidence)

        metadata_store.commit_transaction()
    except BaseException:
        metadata_store.rollback_transaction()
        raise

    stale_hashes = {e["entry_hash"] for e in metadata_store.get_stale_entries()}
    validated = [e for e in entries if hash_entry(e) not in stale_hashes]
    stale = [e for e in entries if hash_entry(e) in stale_hashes]

    return ValidationResult(
        validated_entries=validated,
        stale_filtered=stale,
        validated_count=len(entries),
        stale_count=len(stale),
    )


# -- Phase 2A: Decay + Sweep --


def apply_decay_penalty(
    confidence: float, last_seen: str, current_time: datetime
) -> float:
    """Apply time-based decay to confidence. Separate from compute_confidence (stays pure).

    Grace period: first 14 days, no decay.
    After 14 days: -0.1 per additional 14-day period.
      - Days 15-28: -0.1
      - Days 29-42: -0.2
      - Days 43-56: -0.3
    Clamp to [0.0, 1.0].
    """
    try:
        last_seen_dt = datetime.fromisoformat(last_seen)
    except ValueError:
        return 0.0  # Corrupt timestamp, treat as maximally stale

    # Normalize timezone — metadata.db may have naive timestamps from datetime('now')
    if last_seen_dt.tzinfo is None:
        last_seen_dt = last_seen_dt.replace(tzinfo=timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    days_since = (current_time - last_seen_dt).days
    if days_since <= 14:
        return confidence
    # Subtract grace period, then count 14-day penalty periods
    days_beyond_grace = days_since - 14
    periods = (days_beyond_grace + 13) // 14  # ceiling division
    penalty = 0.1 * periods
    return max(0.0, min(1.0, confidence - penalty))


def _revalidate_citations(
    entry_hash: str, metadata_store: MetadataStore, project_root: Path
) -> list[CheckResult]:
    """Re-validate citations for an entry using DB-stored citation data."""
    rows = metadata_store.conn.execute(
        "SELECT citation_type, citation_value FROM citations WHERE entry_hash = ?",
        (entry_hash,),
    ).fetchall()
    checks: list[CheckResult] = []
    for row in rows:
        citation = Citation(
            type=row["citation_type"],
            value=row["citation_value"],
            raw_match=row["citation_value"],  # Approximation; raw_match not in DB
        )
        result = validate_citation(citation, project_root)
        checks.append(result)
    return checks


@dataclass
class SweepResult:
    """Result of a full sweep across all entries."""

    entries_swept: int
    entries_decayed: int
    entries_marked_for_demotion: int
    demotion_candidates: list[str] = field(default_factory=list)


def sweep_all_entries(
    metadata_store: MetadataStore,
    project_root: Path,
    journal: PromotionJournal | None = None,
) -> SweepResult:
    """Re-validate all entries: recheck citations, apply decay, update stale_streak.

    Includes journal crash recovery and demotion candidate collection.
    """
    # Crash recovery: reconcile journal with DB for incomplete demotions
    if journal is not None:
        for jentry in journal.get_unresolved_demotions():
            row = metadata_store.get_entry(jentry.entry_hash)
            if row and row["status"] != "demoted":
                metadata_store.mark_demoted(jentry.entry_hash)
        metadata_store.conn.commit()  # Commit recovery before sweep transaction

    metadata_store.begin_transaction()
    try:
        # Only sweep active and stale entries (skip demoted, orphaned)
        entries = metadata_store.conn.execute(
            "SELECT * FROM memory_entries WHERE status IN ('active', 'stale')"
        ).fetchall()

        current_time = datetime.now(timezone.utc)
        entries_decayed = 0

        for row in entries:
            entry_hash = row["entry_hash"]
            checks = _revalidate_citations(entry_hash, metadata_store, project_root)
            snapshot_count = row["snapshot_count"]
            base_confidence = compute_confidence(0.5, checks, snapshot_count)

            # NULL last_seen safety
            last_seen = row["last_seen"]
            if last_seen is None:
                last_seen = current_time.isoformat()

            decayed_confidence = apply_decay_penalty(
                base_confidence, last_seen, current_time
            )
            metadata_store.update_confidence(entry_hash, decayed_confidence)

            if abs(decayed_confidence - base_confidence) > 1e-9:
                entries_decayed += 1

            # Hysteresis: track stale streak
            if decayed_confidence < STALE_THRESHOLD:
                metadata_store.increment_stale_streak(entry_hash)
            else:
                metadata_store.reset_stale_streak(entry_hash)

        metadata_store.commit_transaction()
    except Exception:
        metadata_store.rollback_transaction()
        raise

    # Collect demotion candidates from committed state (post-commit = no phantom reads)
    demotion_rows = metadata_store.conn.execute(
        "SELECT entry_hash FROM memory_entries "
        "WHERE stale_streak >= ? AND status != 'demoted'",
        (DEMOTION_STREAK_THRESHOLD,),
    ).fetchall()
    demotion_candidates = [r["entry_hash"] for r in demotion_rows]

    return SweepResult(
        entries_swept=len(entries),
        entries_decayed=entries_decayed,
        entries_marked_for_demotion=len(demotion_candidates),
        demotion_candidates=demotion_candidates,
    )


# -- Standalone validation of already-promoted entries --

# Matches both old (<!-- intermem -->) and new (<!-- intermem:HASH -->) markers
_INTERMEM_MARKER_RE = re.compile(r"\s*<!--\s*intermem(?::([a-f0-9]+))?\s*-->\s*")


def _extract_promoted_entries(target_path: Path) -> list[MemoryEntry]:
    """Extract entries from a target doc that have the intermem marker."""
    if not target_path.exists():
        return []

    entries: list[MemoryEntry] = []
    current_section = target_path.stem
    lines = target_path.read_text(encoding="utf-8").splitlines()

    for i, line in enumerate(lines, start=1):
        if line.startswith("## "):
            current_section = line.lstrip("# ").strip()
            continue

        if _INTERMEM_MARKER_RE.search(line):
            content = _INTERMEM_MARKER_RE.sub("", line).strip()
            if content:
                entries.append(
                    MemoryEntry(
                        content=content,
                        section=current_section,
                        source_file=target_path.name,
                        start_line=i,
                        end_line=i,
                    )
                )

    return entries


def validate_promoted(
    target_docs: list[Path],
    metadata_store: MetadataStore,
    project_root: Path,
) -> ValidatePromotedResult:
    """Validate already-promoted entries in target docs.

    Scans for entries with <!-- intermem --> markers, validates their citations,
    and returns a report.
    """
    all_entries: list[MemoryEntry] = []
    for doc in target_docs:
        all_entries.extend(_extract_promoted_entries(doc))

    if not all_entries:
        return ValidatePromotedResult(
            entries_checked=0, entries_healthy=0, entries_stale=0
        )

    reports: list[PromotedEntryReport] = []
    entries_healthy = 0
    entries_stale = 0

    metadata_store.begin_transaction()
    try:
        for entry in all_entries:
            entry_hash = hash_entry(entry)
            preview = entry.content[:80].replace("\n", " ")
            metadata_store.upsert_entry(
                entry_hash, preview, entry.section, entry.source_file
            )

            citations = extract_citations(entry)
            checks: list[CheckResult] = []
            broken_details: list[str] = []

            for citation in citations:
                resolved = resolve_citation(citation, project_root)
                if resolved is not None:
                    try:
                        resolved_str = str(resolved.relative_to(project_root.resolve()))
                    except ValueError:
                        resolved_str = str(resolved)
                else:
                    resolved_str = None
                metadata_store.record_citation(
                    entry_hash, citation.type, citation.value, resolved_str
                )
                result = validate_citation(citation, project_root)
                metadata_store.record_check(
                    entry_hash,
                    citation.value,
                    citation.type,
                    result.status,
                    result.detail or None,
                )
                checks.append(result)
                if result.status == "broken":
                    broken_details.append(f"{citation.value}: {result.detail}")

            row = metadata_store.get_entry(entry_hash)
            snapshot_count = row["snapshot_count"] if row else 1
            confidence = compute_confidence(0.5, checks, snapshot_count)
            metadata_store.update_confidence(entry_hash, confidence)

            valid_count = sum(1 for c in checks if c.status == "valid")
            broken_count = sum(1 for c in checks if c.status == "broken")

            if confidence < 0.3:
                entries_stale += 1
            else:
                entries_healthy += 1

            reports.append(
                PromotedEntryReport(
                    content_preview=preview,
                    section=entry.section,
                    citations_found=len(citations),
                    citations_valid=valid_count,
                    citations_broken=broken_count,
                    confidence=confidence,
                    broken_details=broken_details,
                )
            )

        metadata_store.commit_transaction()
    except BaseException:
        metadata_store.rollback_transaction()
        raise

    return ValidatePromotedResult(
        entries_checked=len(all_entries),
        entries_healthy=entries_healthy,
        entries_stale=entries_stale,
        reports=reports,
    )
