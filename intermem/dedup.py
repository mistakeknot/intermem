"""Deduplication checker — compares entries against AGENTS.md/CLAUDE.md content."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from intermem.scanner import MemoryEntry

FUZZY_THRESHOLD = 0.75
INTERMEM_MARKER_RE = re.compile(r"\s*<!--\s*intermem\s*-->\s*", re.IGNORECASE)


@dataclass
class DedupResult:
    """Deduplication result for a single entry."""

    entry: MemoryEntry
    status: str  # "novel", "exact_duplicate", "fuzzy_duplicate"
    confidence: float  # 0.0 to 1.0 (1.0 = exact match)
    matched_in: str  # Filename where match was found, or ""
    matched_line: str | None  # The matching line from target doc


def _strip_intermem_marker(text: str) -> str:
    """Remove inline intermem promotion marker comments."""
    return INTERMEM_MARKER_RE.sub(" ", text).strip()


def _normalize(text: str) -> str:
    """Normalize text for comparison: lowercase, strip markdown bullets and whitespace."""
    text = _strip_intermem_marker(text).strip().lower()
    if text.startswith(("- ", "* ")):
        text = text[2:]
    return " ".join(text.split())


def _extract_lines(path: Path) -> list[tuple[str, str, str]]:
    """Extract content lines from a markdown file.

    Returns tuples of (display_line, normalized_source_line, filename).
    """
    if not path.exists():
        return []

    lines: list[tuple[str, str, str]] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        # Skip headings, blank lines, and non-content
        if not stripped or stripped.startswith("#"):
            continue

        comparable = _strip_intermem_marker(stripped)
        if not comparable:
            continue

        lines.append((stripped, comparable, path.name))

    return lines


def check_duplicates(
    entries: list[MemoryEntry],
    target_docs: list[Path],
) -> list[DedupResult]:
    """Check each entry against all target docs for duplicates.

    - Exact hash match -> exact_duplicate (auto-skip)
    - Fuzzy match >= 75% -> fuzzy_duplicate (flag for review)
    - Below threshold -> novel
    """
    target_lines: list[tuple[str, str, str]] = []
    target_hashes: dict[str, tuple[str, str]] = {}  # hash -> (display_line, filename)

    # Build lookup of all target content.
    for doc in target_docs:
        for display_line, comparable_line, filename in _extract_lines(doc):
            norm = _normalize(comparable_line)
            if not norm:
                continue
            line_hash = hashlib.sha256(norm.encode()).hexdigest()[:16]
            target_hashes[line_hash] = (display_line, filename)
            target_lines.append((display_line, filename, norm))

    results: list[DedupResult] = []
    for entry in entries:
        entry_first_line = entry.content.splitlines()[0] if entry.content else ""
        entry_norm = _normalize(entry_first_line)
        entry_hash = hashlib.sha256(entry_norm.encode()).hexdigest()[:16]

        # Check exact hash match.
        if entry_hash in target_hashes:
            display_line, filename = target_hashes[entry_hash]
            results.append(
                DedupResult(
                    entry=entry,
                    status="exact_duplicate",
                    confidence=1.0,
                    matched_in=filename,
                    matched_line=display_line,
                )
            )
            continue

        # Check fuzzy match.
        best_ratio = 0.0
        best_match: tuple[str, str] | None = None
        for display_line, filename, target_norm in target_lines:
            ratio = SequenceMatcher(None, entry_norm, target_norm).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = (display_line, filename)

        if best_ratio >= FUZZY_THRESHOLD and best_match:
            results.append(
                DedupResult(
                    entry=entry,
                    status="fuzzy_duplicate",
                    confidence=best_ratio,
                    matched_in=best_match[1],
                    matched_line=best_match[0],
                )
            )
        else:
            results.append(
                DedupResult(
                    entry=entry,
                    status="novel",
                    confidence=best_ratio,
                    matched_in="",
                    matched_line=best_match[0] if best_match else None,
                )
            )

    return results
