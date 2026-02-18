"""Deduplication checker — compares entries against AGENTS.md/CLAUDE.md content."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from intermem.scanner import MemoryEntry

FUZZY_LINE_THRESHOLD = 0.75
KEYWORD_OVERLAP_THRESHOLD = 0.60
INTERMEM_MARKER_RE = re.compile(r"\s*<!--\s*intermem\s*-->\s*", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z][a-z0-9_.-]{2,}", re.IGNORECASE)


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


def _extract_keywords(text: str) -> set[str]:
    """Extract meaningful keywords (3+ char words) from text, lowercased."""
    return {w.lower() for w in _WORD_RE.findall(text)}


@dataclass
class _TargetSection:
    """A section from a target document with its aggregated keywords."""
    heading: str
    filename: str
    keywords: set[str]
    lines: list[str]  # raw lines for display


def _extract_lines(path: Path) -> list[tuple[str, str, str]]:
    """Extract content lines from a markdown file.

    Returns tuples of (display_line, normalized_source_line, filename).
    """
    if not path.exists():
        return []

    lines: list[tuple[str, str, str]] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        comparable = _strip_intermem_marker(stripped)
        if not comparable:
            continue

        lines.append((stripped, comparable, path.name))

    return lines


def _extract_sections(path: Path) -> list[_TargetSection]:
    """Extract sections from a target document for keyword-level comparison."""
    if not path.exists():
        return []

    sections: list[_TargetSection] = []
    current_heading = path.stem
    current_lines: list[str] = []

    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if current_lines:
                text = "\n".join(current_lines)
                sections.append(_TargetSection(
                    heading=current_heading,
                    filename=path.name,
                    keywords=_extract_keywords(text),
                    lines=current_lines,
                ))
            current_heading = stripped.lstrip("# ").strip()
            current_lines = []
        elif stripped.startswith("# ") and not stripped.startswith("## "):
            if current_lines:
                text = "\n".join(current_lines)
                sections.append(_TargetSection(
                    heading=current_heading,
                    filename=path.name,
                    keywords=_extract_keywords(text),
                    lines=current_lines,
                ))
            current_heading = stripped.lstrip("# ").strip()
            current_lines = []
        elif stripped:
            current_lines.append(_strip_intermem_marker(stripped))

    if current_lines:
        text = "\n".join(current_lines)
        sections.append(_TargetSection(
            heading=current_heading,
            filename=path.name,
            keywords=_extract_keywords(text),
            lines=current_lines,
        ))

    return sections


def check_duplicates(
    entries: list[MemoryEntry],
    target_docs: list[Path],
) -> list[DedupResult]:
    """Check each entry against all target docs for duplicates.

    Three-layer matching:
    1. Exact hash match on normalized first line -> exact_duplicate
    2. Fuzzy line match >= 75% -> fuzzy_duplicate
    3. Keyword overlap: if >= 60% of entry keywords appear in a target section -> fuzzy_duplicate
    """
    target_lines: list[tuple[str, str, str]] = []
    target_hashes: dict[str, tuple[str, str]] = {}

    for doc in target_docs:
        for display_line, comparable_line, filename in _extract_lines(doc):
            norm = _normalize(comparable_line)
            if not norm:
                continue
            line_hash = hashlib.sha256(norm.encode()).hexdigest()[:16]
            target_hashes[line_hash] = (display_line, filename)
            target_lines.append((display_line, filename, norm))

    # Build section-level keyword index for deeper matching.
    target_sections: list[_TargetSection] = []
    for doc in target_docs:
        target_sections.extend(_extract_sections(doc))

    results: list[DedupResult] = []
    for entry in entries:
        entry_first_line = entry.content.splitlines()[0] if entry.content else ""
        entry_norm = _normalize(entry_first_line)
        entry_hash = hashlib.sha256(entry_norm.encode()).hexdigest()[:16]

        # Layer 1: Exact hash match.
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

        # Layer 2: Fuzzy line match.
        best_ratio = 0.0
        best_match: tuple[str, str] | None = None
        for display_line, filename, target_norm in target_lines:
            ratio = SequenceMatcher(None, entry_norm, target_norm).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = (display_line, filename)

        if best_ratio >= FUZZY_LINE_THRESHOLD and best_match:
            results.append(
                DedupResult(
                    entry=entry,
                    status="fuzzy_duplicate",
                    confidence=best_ratio,
                    matched_in=best_match[1],
                    matched_line=best_match[0],
                )
            )
            continue

        # Layer 3: Keyword overlap against target sections.
        entry_full_text = _normalize(entry.content)
        entry_keywords = _extract_keywords(entry_full_text)
        if entry_keywords:
            best_overlap = 0.0
            best_section: _TargetSection | None = None
            for section in target_sections:
                if not section.keywords:
                    continue
                overlap = len(entry_keywords & section.keywords) / len(entry_keywords)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_section = section

            if best_overlap >= KEYWORD_OVERLAP_THRESHOLD and best_section:
                matched_display = f"[section: {best_section.heading}]"
                if best_section.lines:
                    matched_display = best_section.lines[0]
                results.append(
                    DedupResult(
                        entry=entry,
                        status="fuzzy_duplicate",
                        confidence=best_overlap,
                        matched_in=best_section.filename,
                        matched_line=matched_display,
                    )
                )
                continue

        results.append(
            DedupResult(
                entry=entry,
                status="novel",
                confidence=max(best_ratio, best_overlap if entry_keywords else 0.0),
                matched_in="",
                matched_line=best_match[0] if best_match else None,
            )
        )

    return results
