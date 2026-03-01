"""Structural tidy for auto-memory files — section extraction and stale count detection."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


# Patterns that represent point-in-time counts likely to go stale.
_COUNT_RE = re.compile(
    r"(\d+)\s+(?:\w+\s+)?(plugins?|commands?|skills?|tests?|files?|modules?"
    r"|tools?|agents?|hooks?|servers?|projects?|repos?|pillars?|layers?)",
    re.IGNORECASE,
)

# Sections that should never be extracted to topic files.
_PROTECTED_SECTIONS = {"quick reference", "topic files"}

_SECTION_RE = re.compile(r"^##\s+(.+)$")


@dataclass
class Extraction:
    """A section to be extracted to a topic file."""
    section: str
    slug: str
    line_count: int
    skipped: bool = False  # True if topic file already exists


@dataclass
class StaleCount:
    """A detected count pattern that may be stale."""
    text: str
    stored_count: int
    actual_count: int | None  # None if not verifiable
    source_file: str
    line_number: int


@dataclass
class TidyResult:
    """Result of a tidy operation."""
    memory_file: str
    total_lines: int
    budget: int
    under_budget: bool
    extractions: list[Extraction] = field(default_factory=list)
    stale_counts: list[StaleCount] = field(default_factory=list)
    new_line_count: int = 0


@dataclass
class _Section:
    """Internal: a parsed section of MEMORY.md."""
    name: str
    start_line: int  # 0-indexed line where ## heading is
    end_line: int     # 0-indexed exclusive end
    lines: list[str]  # includes the ## heading


def _slugify(name: str) -> str:
    """Convert a section name to a filename-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s-]+", "-", slug)
    slug = slug.strip("-")
    return slug or "untitled"


def _parse_sections(lines: list[str]) -> list[_Section]:
    """Parse MEMORY.md lines into sections."""
    sections: list[_Section] = []
    current_name: str | None = None
    current_start = 0
    current_lines: list[str] = []

    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if m:
            if current_name is not None:
                sections.append(_Section(
                    name=current_name,
                    start_line=current_start,
                    end_line=i,
                    lines=current_lines,
                ))
            current_name = m.group(1).strip()
            current_start = i
            current_lines = [line]
        else:
            if current_name is not None:
                current_lines.append(line)
            # Lines before any section heading are preamble — keep in place

    if current_name is not None:
        sections.append(_Section(
            name=current_name,
            start_line=current_start,
            end_line=len(lines),
            lines=current_lines,
        ))

    return sections


def _build_topic_link(section_name: str, slug: str) -> str:
    """Build a markdown link for the Topic Files section."""
    return f"- [{slug}.md]({slug}.md) — {section_name}"


def tidy_memory(
    memory_dir: Path,
    *,
    budget: int = 120,
    section_threshold: int = 15,
    apply: bool = False,
) -> TidyResult:
    """Tidy MEMORY.md by extracting oversized sections to topic files.

    Args:
        memory_dir: Path to the auto-memory directory
        budget: Maximum line count for MEMORY.md
        section_threshold: Minimum section size to consider for extraction
        apply: If True, write changes. If False, dry-run only.

    Returns:
        TidyResult with extraction details and projected line count.
    """
    memory_file = memory_dir / "MEMORY.md"
    if not memory_file.exists():
        return TidyResult(
            memory_file=str(memory_file),
            total_lines=0,
            budget=budget,
            under_budget=True,
        )

    text = memory_file.read_text()
    lines = text.splitlines()
    total_lines = len(lines)

    if total_lines <= budget:
        stale_counts = detect_stale_counts(memory_dir)
        return TidyResult(
            memory_file=str(memory_file),
            total_lines=total_lines,
            budget=budget,
            under_budget=True,
            stale_counts=stale_counts,
            new_line_count=total_lines,
        )

    sections = _parse_sections(lines)
    extractions: list[Extraction] = []

    # Sort by line count descending — extract biggest sections first
    extractable = [
        s for s in sections
        if s.name.lower() not in _PROTECTED_SECTIONS and len(s.lines) >= section_threshold
    ]
    extractable.sort(key=lambda s: len(s.lines), reverse=True)

    lines_freed = 0
    extracted_sections: set[str] = set()

    for section in extractable:
        slug = _slugify(section.name)
        topic_file = memory_dir / f"{slug}.md"

        if topic_file.exists():
            extractions.append(Extraction(
                section=section.name,
                slug=slug,
                line_count=len(section.lines),
                skipped=True,
            ))
            continue

        extractions.append(Extraction(
            section=section.name,
            slug=slug,
            line_count=len(section.lines),
        ))
        extracted_sections.add(section.name)

        # Count lines freed: section lines minus 1 line for the topic link
        lines_freed += len(section.lines) - 1

        if apply:
            # Write content to topic file (skip the ## heading, add # heading)
            content_lines = section.lines[1:]  # skip the ## heading
            # Strip leading blank lines
            while content_lines and not content_lines[0].strip():
                content_lines = content_lines[1:]
            topic_content = f"# {section.name}\n\n" + "\n".join(content_lines) + "\n"
            topic_file.write_text(topic_content)

        # Check if we've freed enough
        if total_lines - lines_freed <= budget:
            break

    # Calculate new line count: add lines for Topic Files section if creating
    topic_files_exists = any(s.name.lower() == "topic files" for s in sections)
    new_links = [e for e in extractions if not e.skipped]
    topic_section_overhead = 0 if topic_files_exists else 2  # "## Topic Files\n\n"
    new_line_count = total_lines - lines_freed + topic_section_overhead

    if apply and extracted_sections:
        _rewrite_memory_file(memory_file, lines, sections, extracted_sections, new_links)

    stale_counts = detect_stale_counts(memory_dir)

    return TidyResult(
        memory_file=str(memory_file),
        total_lines=total_lines,
        budget=budget,
        under_budget=False,
        extractions=extractions,
        stale_counts=stale_counts,
        new_line_count=new_line_count,
    )


def _rewrite_memory_file(
    memory_file: Path,
    original_lines: list[str],
    sections: list[_Section],
    extracted_sections: set[str],
    new_links: list[Extraction],
) -> None:
    """Rewrite MEMORY.md with extracted sections replaced by topic links."""
    # Find or create Topic Files section
    topic_section_idx: int | None = None
    for i, section in enumerate(sections):
        if section.name.lower() == "topic files":
            topic_section_idx = i
            break

    # Build new file content
    result_lines: list[str] = []

    # Add preamble (lines before first section)
    if sections:
        preamble_end = sections[0].start_line
        result_lines.extend(original_lines[:preamble_end])

    for i, section in enumerate(sections):
        if section.name in extracted_sections:
            continue  # Skip extracted sections

        if section.name.lower() == "topic files":
            # Append new links to existing Topic Files section
            result_lines.extend(section.lines)
            # Strip trailing blank lines before adding links
            while result_lines and not result_lines[-1].strip():
                result_lines.pop()
            for ext in new_links:
                result_lines.append(_build_topic_link(ext.section, ext.slug))
            result_lines.append("")
        else:
            result_lines.extend(section.lines)

    # If no Topic Files section existed, add one at the end
    if topic_section_idx is None and new_links:
        # Ensure blank line before new section
        if result_lines and result_lines[-1].strip():
            result_lines.append("")
        result_lines.append("## Topic Files")
        result_lines.append("")
        for ext in new_links:
            result_lines.append(_build_topic_link(ext.section, ext.slug))
        result_lines.append("")

    memory_file.write_text("\n".join(result_lines) + "\n")


def detect_stale_counts(
    memory_dir: Path,
    project_dir: Path | None = None,
) -> list[StaleCount]:
    """Scan memory files for point-in-time count patterns that may be stale.

    Checks all .md files in memory_dir for patterns like "48 plugins".
    For counts that reference countable directories (relative to project_dir),
    resolves the actual count.

    Args:
        memory_dir: Path to the auto-memory directory
        project_dir: Project root for resolving countable paths. If None, counts
                     are flagged but not verified.

    Returns:
        List of StaleCount instances.
    """
    results: list[StaleCount] = []

    md_files = sorted(memory_dir.glob("*.md"))
    for md_file in md_files:
        text = md_file.read_text()
        for i, line in enumerate(text.splitlines(), start=1):
            for m in _COUNT_RE.finditer(line):
                stored = int(m.group(1))
                noun = m.group(2).lower()
                matched_text = m.group(0)

                actual: int | None = None
                if project_dir:
                    actual = _try_count(project_dir, noun, stored)

                results.append(StaleCount(
                    text=matched_text,
                    stored_count=stored,
                    actual_count=actual,
                    source_file=md_file.name,
                    line_number=i,
                ))

    return results


def _try_count(project_dir: Path, noun: str, _stored: int) -> int | None:
    """Try to verify a count by checking the filesystem.

    Returns the actual count if verifiable, None otherwise.
    """
    # Map common nouns to directories/patterns
    noun_singular = noun.rstrip("s") if noun.endswith("s") and noun != "lass" else noun
    dir_mappings: dict[str, list[Path]] = {
        "plugin": [project_dir / "interverse"],
        "skill": [],  # Would need to scan all plugins — skip
        "hook": [],
        "agent": [],
        "module": [project_dir / "interverse"],
        "test": [],  # Would need to run tests — skip
        "file": [],
        "command": [],
        "server": [],
        "project": [],
        "repo": [],
        "pillar": [],
        "layer": [],
        "tool": [],
    }

    dirs = dir_mappings.get(noun_singular, [])
    for d in dirs:
        if d.exists() and d.is_dir():
            # Count immediate subdirectories (for plugins, modules, etc.)
            count = sum(1 for p in d.iterdir() if p.is_dir() and not p.name.startswith("."))
            return count

    return None
