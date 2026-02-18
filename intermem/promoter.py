"""Promoter — writes approved entries to AGENTS.md/CLAUDE.md with journal tracking."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from intermem.journal import PromotionJournal
from intermem.metadata import MetadataStore
from intermem.scanner import MemoryEntry
from intermem._util import hash_content, hash_entry

# Old-style marker kept as fallback for detection.
MARKER = "<!-- intermem -->"

# Regex matches both old (<!-- intermem -->) and new (<!-- intermem:HASH -->) markers.
_INTERMEM_MARKER_RE = re.compile(r"<!--\s*intermem(?::([a-f0-9]+))?\s*-->")


def make_marker(entry_hash: str | None = None) -> str:
    """Create an intermem marker, optionally embedding a truncated entry hash."""
    if entry_hash:
        return f"<!-- intermem:{entry_hash[:8]} -->"
    return MARKER


@dataclass
class PromotionResult:
    """Summary of a promotion operation."""

    promoted_count: int
    sections_modified: list[str]
    sections_created: list[str]


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
            entry_hash = hash_content(entry.content)
            journal.record_pending(entry_hash, target_path.name, section_name, entry.content)
            all_hashes.append(entry_hash)

    # Accumulate all section mutations in memory.
    for section_name, section_entries in by_section.items():
        section_pattern = re.compile(
            rf"^(## {re.escape(section_name)}\s*\n)(.*?)(?=^## |\Z)",
            re.MULTILINE | re.DOTALL,
        )
        match = section_pattern.search(content)

        insert_lines = [
            f"{entry.content} {make_marker(hash_entry(entry))}"
            for entry in section_entries
        ]
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


# -- Phase 2A: Demotion --


@dataclass
class DemotionResult:
    """Summary of a demotion operation."""

    demoted_count: int
    files_modified: list[str] = field(default_factory=list)


def demote_entries(
    entry_hashes: list[str],
    metadata_store: MetadataStore,
    target_docs: list[Path],
    journal: PromotionJournal,
) -> DemotionResult:
    """Remove stale entries from target docs by hash-based marker matching.

    Four-phase ordering for crash safety:
      1. Scan files + record intent in journal (journal-first)
      2. Write modified files (irreversible user-visible change)
      3. Update DB status (mark_demoted)
      4. Mark journal entries as committed (idempotent)
    Crash recovery: sweep checks for recorded-but-uncommitted demotions.
    Old-style markers (without hash) are never demoted — only hash-based markers are matched.
    """
    if not entry_hashes:
        return DemotionResult(demoted_count=0)

    # Pre-compute truncated hashes for matching
    short_to_full = {h[:8]: h for h in entry_hashes}

    # Phase 1: Scan files and record intent in journal (crash-safe: journal-first)
    # Collect per-file removal plans without modifying anything yet.
    file_plans: list[tuple[Path, list[str], list[str]]] = []  # (doc, new_lines, demoted_hashes)

    for doc in target_docs:
        if not doc.exists():
            continue
        text = doc.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        new_lines: list[str] = []
        demoted_in_doc: list[str] = []

        for line in lines:
            match = _INTERMEM_MARKER_RE.search(line)
            if match:
                marker_hash = match.group(1)  # None for old-style markers
                if marker_hash and marker_hash in short_to_full:
                    full_hash = short_to_full[marker_hash]
                    content = _INTERMEM_MARKER_RE.sub("", line).strip()
                    journal.record_demoted(full_hash, str(doc), "", content)
                    demoted_in_doc.append(full_hash)
                    continue  # Remove this line from output
            new_lines.append(line)

        if demoted_in_doc:
            file_plans.append((doc, new_lines, demoted_in_doc))

    # Phase 2: Write modified files (the irreversible user-visible change)
    demoted_count = 0
    files_modified: list[str] = []
    all_demoted_hashes: list[str] = []

    for doc, new_lines, demoted_hashes in file_plans:
        doc.write_text("".join(new_lines), encoding="utf-8")
        files_modified.append(str(doc))
        demoted_count += len(demoted_hashes)
        all_demoted_hashes.extend(demoted_hashes)

    # Phase 3: Update DB status (safe: file already written, journal has intent)
    for full_hash in all_demoted_hashes:
        metadata_store.mark_demoted(full_hash)

    # Phase 4: Mark journal entries as committed (idempotent)
    for full_hash in all_demoted_hashes:
        journal.mark_demotion_committed(full_hash)

    return DemotionResult(demoted_count=demoted_count, files_modified=files_modified)
