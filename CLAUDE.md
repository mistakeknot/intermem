# intermem

Memory synthesis plugin — graduates stable auto-memory facts to curated reference documents.

## Quick Reference

- **Plugin manifest**: `.claude-plugin/plugin.json`
- **Library**: `intermem/` (Python, run via `uv run`)
- **State directory**: `.intermem/` in target project root
- **Skills**: `/intermem:synthesize`, `/intermem:validate`

## Architecture

Pipeline: scan → stability → **validate** → dedup → approve → promote+prune

- `intermem/scanner.py` — Parse auto-memory markdown into structured entries
- `intermem/stability.py` — Per-entry content hashing across snapshots
- `intermem/citations.py` — Citation extraction, path resolution, validation, confidence scoring (pure functions)
- `intermem/metadata.py` — SQLite store for entry provenance, citations, audit checks
- `intermem/validator.py` — Validation pipeline: `validate_and_filter_entries()`, `validate_promoted()`
- `intermem/dedup.py` — Fuzzy matching against AGENTS.md/CLAUDE.md
- `intermem/promoter.py` — Write entries to target docs with marker comments
- `intermem/pruner.py` — Remove promoted entries from auto-memory
- `intermem/journal.py` — WAL-style promotion journal for atomicity
- `intermem/_util.py` — Shared utilities: `hash_entry()`, `hash_content()`, `normalize_content()`

## State Files

- `.intermem/stability.jsonl` — Per-entry content hash history
- `.intermem/promotion-journal.jsonl` — Atomic promotion/prune log
- `.intermem/metadata.db` — SQLite: entry provenance, citations, confidence scores, audit trail

## Design Decisions

- **Hash length**: SHA-256 truncated to 16 hex chars. Collision risk negligible at <1000 entries per project. Kept for backward compatibility with stability.jsonl.
- **Dual state stores**: metadata.db is additive alongside JSONL. Deleting it restores Phase 0.5 behavior. Phase 2 may consolidate.
- **Confidence scoring**: Base 0.5, +0.3 valid citation, -0.4 broken, +0.2 for 5+ snapshots. Threshold < 0.3 = stale.
- **Path safety**: `Path.resolve()` + `is_relative_to()` for all citation paths. Symlinks dereferenced. Resolved paths stored as relative in SQLite.
- **Regex safety**: `_ABSOLUTE_PATH_RE` capped at 512 chars, `extract_citations()` caps input at 50KB.
- **Citation idempotency**: `UNIQUE(entry_hash, citation_value)` + `ON CONFLICT DO UPDATE` prevents unbounded row growth across runs.
- **Foreign keys**: `ON DELETE CASCADE` on citations and citation_checks → memory_entries.

## Constraints

- No hooks (Clavain hook budget)
- No MCP server (skill-only)
- `.intermem/` must be in `.gitignore` for the target project
- Python stdlib only (sqlite3 is stdlib)
