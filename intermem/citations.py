"""Citation extraction, resolution, validation, and confidence scoring.

Pure functions — no database I/O, no side effects beyond os.path.exists().
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from intermem.scanner import MemoryEntry

# -- Dataclasses --


@dataclass
class Citation:
    """A reference extracted from a memory entry."""

    type: str  # 'file_path' or 'module'
    value: str  # The extracted path/reference
    raw_match: str  # Original matched text


@dataclass
class CheckResult:
    """Result of validating a single citation."""

    status: str  # 'valid', 'broken', 'unchecked'
    confidence_delta: float
    detail: str


# -- Extraction --

# Backtick-quoted paths: `path/to/file.ext` (must contain /)
_BACKTICK_PATH_RE = re.compile(r"`([^\s`]+/[^\s`]+)`")

# Markdown link paths: [text](path/to/file)
_MARKDOWN_LINK_RE = re.compile(r"\]\(([^)]+/[^)]+)\)")

# Absolute paths starting with / (e.g., /root/projects/...)
# Length-capped to 512 chars to prevent regex DoS on crafted input.
_ABSOLUTE_PATH_RE = re.compile(r"(?<!\w)(/(?:root|home|usr|opt|etc|var|tmp)/\S{1,512})")

# Patterns that look like paths but aren't.
_EXCLUDE_PATTERNS = [
    re.compile(r"^https?://"),  # URLs
    re.compile(r"^\w+://"),  # Other protocols (ftp://, ssh://, etc.)
    re.compile(r"^[A-Z_]+="),  # Environment variables (VAR=value)
    re.compile(r"^--"),  # CLI flags
    re.compile(r"^\$"),  # Shell variables
    re.compile(r"^[<>|&]"),  # Shell operators
]

# Known file extensions for classification.
_FILE_EXTENSIONS = {
    ".py", ".ts", ".js", ".go", ".rs", ".md", ".json", ".yaml", ".yml",
    ".toml", ".sh", ".bash", ".sql", ".html", ".css", ".txt", ".cfg",
    ".jsonl", ".db", ".lock", ".env",
}


def _is_excluded(value: str) -> bool:
    """Check if a matched value is a known non-path pattern."""
    return any(pat.match(value) for pat in _EXCLUDE_PATTERNS)


def _classify_citation(value: str) -> str:
    """Classify a citation as 'file_path' or 'module'."""
    _, ext = os.path.splitext(value)
    if ext.lower() in _FILE_EXTENSIONS:
        return "file_path"
    # If it contains a dot in the last component, likely a file.
    basename = os.path.basename(value)
    if "." in basename:
        return "file_path"
    return "module"


# Maximum content size for citation extraction (50 KB).
_MAX_CONTENT_LEN = 50_000


def extract_citations(entry: MemoryEntry) -> list[Citation]:
    """Extract file path and module references from entry content."""
    content = entry.content[:_MAX_CONTENT_LEN] if len(entry.content) > _MAX_CONTENT_LEN else entry.content
    seen: set[str] = set()
    citations: list[Citation] = []

    for pattern in (_BACKTICK_PATH_RE, _MARKDOWN_LINK_RE, _ABSOLUTE_PATH_RE):
        for match in pattern.finditer(content):
            raw = match.group(0)
            value = match.group(1)
            # Clean trailing punctuation that's not part of the path.
            value = value.rstrip(".,;:)>\"'")

            if _is_excluded(value):
                continue
            if value in seen:
                continue
            seen.add(value)

            citation_type = _classify_citation(value)
            citations.append(Citation(type=citation_type, value=value, raw_match=raw))

    return citations


# -- Resolution --


def resolve_citation(citation: Citation, project_root: Path) -> Path | None:
    """Resolve a citation path against the project root.

    Returns the canonical resolved path, or None if:
    - The path escapes the project boundary
    - The path can't be resolved
    """
    try:
        canonical_root = project_root.resolve()
        resolved = (canonical_root / citation.value).resolve()
    except (OSError, ValueError):
        return None

    if not resolved.is_relative_to(canonical_root):
        return None
    return resolved


# -- Validation --


def validate_citation(citation: Citation, project_root: Path) -> CheckResult:
    """Validate a citation by checking if the referenced artifact exists."""
    resolved = resolve_citation(citation, project_root)
    if resolved is None:
        return CheckResult(
            status="unchecked",
            confidence_delta=0.0,
            detail="Cannot resolve path",
        )

    if citation.type == "file_path":
        if resolved.exists():
            return CheckResult(status="valid", confidence_delta=0.3, detail="")
        return CheckResult(
            status="broken",
            confidence_delta=-0.4,
            detail="File not found",
        )

    if citation.type == "module":
        if resolved.is_dir():
            return CheckResult(status="valid", confidence_delta=0.3, detail="")
        return CheckResult(
            status="broken",
            confidence_delta=-0.4,
            detail="Directory not found",
        )

    return CheckResult(
        status="unchecked",
        confidence_delta=0.0,
        detail=f"Unknown citation type: {citation.type}",
    )


# -- Confidence --


def compute_confidence(
    base: float,
    checks: list[CheckResult],
    snapshot_count: int,
) -> float:
    """Compute entry confidence from citation checks and stability signals.

    Additive model with hard clamp to [0.0, 1.0]:
    - base: starting confidence (typically 0.5)
    - Each check adds its confidence_delta (+0.3 valid, -0.4 broken, 0.0 unchecked)
    - snapshot_count >= 5: +0.2 stability bonus
    """
    confidence = base
    for check in checks:
        confidence += check.confidence_delta

    if snapshot_count >= 5:
        confidence += 0.2

    return max(0.0, min(1.0, confidence))
