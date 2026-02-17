"""Promoter — writes approved entries to AGENTS.md/CLAUDE.md with journal tracking."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from intermem.journal import PromotionJournal
from intermem.scanner import MemoryEntry

MARKER = "<!-- intermem -->"


@dataclass
class PromotionResult:
    """Summary of a promotion operation."""

    promoted_count: int
    sections_modified: list[str]
    sections_created: list[str]


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.strip().encode()).hexdigest()[:16]


def promote_entries(
    entries: list[MemoryEntry],
    target_path: Path,
    journal: PromotionJournal,
) -> PromotionResult:
    """Write entries to the target document, grouped by section."""
    if not entries:
        return PromotionResult(promoted_count=0, sections_modified=[], sections_created=[])

    by_section: dict[str, list[MemoryEntry]] = {}
    for entry in entries:
        by_section.setdefault(entry.section, []).append(entry)

    if target_path.exists():
        content = target_path.read_text()
    else:
        content = f"# {target_path.stem}\n"

    sections_modified: list[str] = []
    sections_created: list[str] = []

    # Record all entries as pending first.
    all_hashes: list[str] = []
    for section_name, section_entries in by_section.items():
        for entry in section_entries:
            entry_hash = _hash_content(entry.content)
            journal.record_pending(entry_hash, target_path.name, section_name, entry.content)
            all_hashes.append(entry_hash)

    # Accumulate all section mutations in memory.
    for section_name, section_entries in by_section.items():
        section_pattern = re.compile(
            rf"^(## {re.escape(section_name)}\s*\n)(.*?)(?=^## |\Z)",
            re.MULTILINE | re.DOTALL,
        )
        match = section_pattern.search(content)

        insert_lines = [f"{entry.content} {MARKER}" for entry in section_entries]
        insert_text = "\n".join(insert_lines) + "\n"

        if match:
            section_body = match.group(2)
            insert_pos = match.end(2)
            prefix = "" if section_body.endswith("\n") or section_body == "" else "\n"
            content = content[:insert_pos] + prefix + insert_text + content[insert_pos:]
            sections_modified.append(section_name)
        else:
            if not content.endswith("\n"):
                content += "\n"
            content += f"\n## {section_name}\n{insert_text}"
            sections_created.append(section_name)

    # Single atomic write, then mark all committed.
    target_path.write_text(content)
    for entry_hash in all_hashes:
        journal.mark_committed(entry_hash)

    return PromotionResult(
        promoted_count=len(entries),
        sections_modified=sections_modified,
        sections_created=sections_created,
    )
