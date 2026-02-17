"""Auto-memory pruner — removes promoted entries from source files."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from intermem.journal import PromotionJournal
from intermem.scanner import MemoryEntry, scan_memory_dir
from intermem._util import normalize_content


@dataclass
class PruneResult:
    """Summary of a prune operation."""

    lines_removed: int
    new_total_lines: int
    dry_run: bool = False


def _find_block(lines: list[str], block: list[str]) -> int | None:
    """Find the first contiguous block match in lines (whitespace-insensitive)."""
    if not block:
        return None

    block_len = len(block)
    normalized_block = [line.strip() for line in block]
    limit = len(lines) - block_len + 1
    for idx in range(max(0, limit)):
        candidate = lines[idx : idx + block_len]
        if [line.strip() for line in candidate] == normalized_block:
            return idx
    return None


def _clean_orphaned_headers(lines: list[str]) -> list[str]:
    """Remove second-level section headers that no longer contain content."""
    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("## "):
            j = i + 1
            has_content = False
            while j < len(lines):
                probe = lines[j]
                if probe.startswith("## ") or probe.startswith("# "):
                    break
                if probe.strip():
                    has_content = True
                    break
                j += 1

            if not has_content:
                while result and not result[-1].strip():
                    result.pop()
                i += 1
                while i < len(lines) and not lines[i].strip():
                    i += 1
                continue

        result.append(line)
        i += 1

    return result


def _journal_committed_lookup(journal: PromotionJournal) -> dict[str, list[str]]:
    """Map normalized content to committed journal hashes."""
    lookup: dict[str, list[str]] = {}
    for item in journal.get_incomplete():
        if item.status != "committed":
            continue
        key = normalize_content(item.content)
        lookup.setdefault(key, []).append(item.entry_hash)
    return lookup


def prune_promoted(
    entries: list[MemoryEntry],
    memory_dir: Path,
    journal: PromotionJournal,
    dry_run: bool = False,
) -> PruneResult:
    """Remove committed promoted entries from auto-memory files."""
    committed_lookup = _journal_committed_lookup(journal)
    by_file: dict[str, list[MemoryEntry]] = {}
    for entry in entries:
        key = normalize_content(entry.content)
        if key not in committed_lookup:
            continue
        by_file.setdefault(entry.source_file, []).append(entry)

    if not by_file:
        current_total = scan_memory_dir(memory_dir).total_lines
        return PruneResult(lines_removed=0, new_total_lines=current_total, dry_run=dry_run)

    total_removed = 0
    file_deltas: dict[str, tuple[int, int]] = {}
    hashes_to_mark: set[str] = set()

    for source_file, file_entries in by_file.items():
        file_path = memory_dir / source_file
        if not file_path.exists():
            continue

        original_text = file_path.read_text(encoding="utf-8")
        original_lines = original_text.splitlines()
        working_lines = list(original_lines)
        removed_keys: set[str] = set()

        for entry in file_entries:
            entry_block = entry.content.splitlines()
            match_index = _find_block(working_lines, entry_block)
            if match_index is None:
                continue

            del working_lines[match_index : match_index + len(entry_block)]
            if match_index < len(working_lines) and not working_lines[match_index].strip():
                del working_lines[match_index]

            removed_keys.add(normalize_content(entry.content))

        cleaned_lines = _clean_orphaned_headers(working_lines)
        while cleaned_lines and not cleaned_lines[-1].strip():
            cleaned_lines.pop()

        removed_count = max(0, len(original_lines) - len(cleaned_lines))
        total_removed += removed_count
        file_deltas[source_file] = (len(original_lines), len(cleaned_lines))

        for key in removed_keys:
            hashes_to_mark.update(committed_lookup.get(key, []))

        if dry_run:
            continue

        backup_path = file_path.with_suffix(file_path.suffix + ".bak")
        backup_path.write_text(original_text, encoding="utf-8")

        if cleaned_lines:
            file_path.write_text("\n".join(cleaned_lines) + "\n", encoding="utf-8")
        else:
            file_path.write_text("", encoding="utf-8")

    if not dry_run:
        for entry_hash in sorted(hashes_to_mark):
            try:
                journal.mark_pruned(entry_hash)
            except KeyError:
                continue

    current_total = scan_memory_dir(memory_dir).total_lines
    if dry_run:
        delta = sum(new - old for old, new in file_deltas.values())
        new_total = max(0, current_total + delta)
    else:
        new_total = current_total

    return PruneResult(
        lines_removed=total_removed,
        new_total_lines=new_total,
        dry_run=dry_run,
    )
