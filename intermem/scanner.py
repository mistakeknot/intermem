"""Auto-memory scanner — reads and parses project memory files into structured entries."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MemoryEntry:
    """A single fact/entry parsed from auto-memory."""
    content: str
    section: str
    source_file: str
    start_line: int
    end_line: int


@dataclass
class ScanResult:
    """Result of scanning a memory directory."""
    entries: list[MemoryEntry] = field(default_factory=list)
    total_lines: int = 0
    near_cap: bool = False  # True when total_lines > 150


def scan_memory_dir(memory_dir: Path) -> ScanResult:
    """Scan all .md files in memory_dir, parse into structured entries.

    Parsing rules:
    - ## headings define sections
    - Bullet points (- or *) at indent level 0 start new entries
    - Continuation lines (indented, code blocks) belong to the current entry
    - Lines before any ## heading use filename as section name
    """
    entries: list[MemoryEntry] = []
    total_lines = 0

    md_files = sorted(memory_dir.glob("*.md"))
    for md_file in md_files:
        text = md_file.read_text()
        lines = text.splitlines()
        total_lines += len(lines)
        file_entries = _parse_file(lines, md_file.name)
        entries.extend(file_entries)

    return ScanResult(
        entries=entries,
        total_lines=total_lines,
        near_cap=total_lines > 150,
    )


def _parse_file(lines: list[str], filename: str) -> list[MemoryEntry]:
    """Parse a single markdown file into entries."""
    entries: list[MemoryEntry] = []
    current_section = filename.removesuffix(".md")
    current_entry_lines: list[str] = []
    entry_start_line = 0
    in_code_block = False

    for i, line in enumerate(lines, start=1):
        # Track code blocks
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            if current_entry_lines:
                current_entry_lines.append(line)
            continue

        if in_code_block:
            if current_entry_lines:
                current_entry_lines.append(line)
            continue

        # Section heading
        if line.startswith("## "):
            # Flush current entry
            if current_entry_lines:
                entries.append(_make_entry(
                    current_entry_lines, current_section, filename, entry_start_line, i - 1
                ))
                current_entry_lines = []
            current_section = line.lstrip("# ").strip()
            continue

        # Skip top-level headings (# Title)
        if line.startswith("# ") and not line.startswith("## "):
            if current_entry_lines:
                entries.append(_make_entry(
                    current_entry_lines, current_section, filename, entry_start_line, i - 1
                ))
                current_entry_lines = []
            continue

        # New bullet entry at root level
        if re.match(r'^[-*] ', line):
            if current_entry_lines:
                entries.append(_make_entry(
                    current_entry_lines, current_section, filename, entry_start_line, i - 1
                ))
            current_entry_lines = [line]
            entry_start_line = i
            continue

        # Continuation line (indented or empty within entry)
        if current_entry_lines:
            current_entry_lines.append(line)
        # Else: skip blank/preamble lines outside entries

    # Flush final entry
    if current_entry_lines:
        entries.append(_make_entry(
            current_entry_lines, current_section, filename, entry_start_line, len(lines)
        ))

    return entries


def _make_entry(
    lines: list[str], section: str, filename: str, start: int, end: int
) -> MemoryEntry:
    """Create a MemoryEntry from accumulated lines."""
    # Strip trailing blank lines (copy to avoid mutating caller's list)
    lines = list(lines)
    while lines and not lines[-1].strip():
        lines.pop()
        end -= 1
    return MemoryEntry(
        content="\n".join(lines),
        section=section,
        source_file=filename,
        start_line=start,
        end_line=end,
    )
