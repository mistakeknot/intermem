# intermem

Memory synthesis plugin — graduates stable auto-memory facts to curated reference documents.

## Quick Reference

- **Plugin manifest**: `.claude-plugin/plugin.json`
- **Library**: `intermem/` (Python, run via `uv run`)
- **State directory**: `.intermem/` in target project root
- **Skills**: `/intermem:synthesize`, `/intermem:validate`, `/intermem:tidy`
- **CLI**: `uv run python -m intermem [--flags] [sweep|query|tidy]`

## Architecture

Pipeline: scan → stability → **validate** → dedup → approve → promote+prune

Secondary pipeline (Phase 2A): sweep → decay → demotion → (re-promotion on reappearance)

- `intermem/scanner.py` — Parse auto-memory markdown into structured entries
- `intermem/stability.py` — Per-entry content hashing across snapshots
- `intermem/citations.py` — Citation extraction, path resolution, validation, confidence scoring (pure functions)
- `intermem/metadata.py` — SQLite store for entry provenance, citations, audit checks, stale_streak, demotion
- `intermem/validator.py` — Validation pipeline + decay penalty + sweep: `validate_and_filter_entries()`, `validate_promoted()`, `apply_decay_penalty()`, `sweep_all_entries()`
- `intermem/dedup.py` — Fuzzy matching against AGENTS.md/CLAUDE.md
- `intermem/promoter.py` — Write entries with hash-based markers, demote stale entries: `promote_entries()`, `demote_entries()`
- `intermem/pruner.py` — Remove promoted entries from auto-memory
- `intermem/journal.py` — WAL-style promotion/demotion journal for atomicity
- `intermem/tidy.py` — Structural tidy: section extraction to topic files, stale count detection
- `intermem/_util.py` — Shared utilities: `hash_entry()`, `hash_content()`, `normalize_content()`

## CLI Subcommands

- `intermem sweep` — Re-validate all entries, apply time-based decay, demote stale entries from target docs
- `intermem query --search <keyword>` — Search entries by keyword in content/section
- `intermem query --topics` — List topic sections with entry counts and avg confidence
- `intermem query --demoted` — Show demoted entries
- `intermem tidy [--budget N] [--section-threshold N] [--apply]` — Structural tidy: extract oversized sections to topic files, detect stale counts
- No subcommand — backward-compatible synthesis mode (all existing flags work)

## State Files

- `.intermem/stability.jsonl` — Per-entry content hash history
- `.intermem/promotion-journal.jsonl` — Atomic promotion/prune/demotion log
- `.intermem/metadata.db` — SQLite: entry provenance, citations, confidence, stale_streak, demotion status

## Design Decisions

- **Hash length**: SHA-256 truncated to 16 hex chars. Collision risk negligible at <1000 entries per project. Kept for backward compatibility with stability.jsonl.
- **Hash-based markers**: Promoted entries use `<!-- intermem:HASH -->` (first 8 chars of entry hash) for deterministic demotion matching. Old `<!-- intermem -->` markers are still detected but never auto-demoted.
- **Dual state stores**: metadata.db is additive alongside JSONL. Deleting it restores Phase 0.5 behavior.
- **Confidence scoring**: Base 0.5, +0.3 valid citation, -0.4 broken, +0.2 for 5+ snapshots. Threshold < 0.3 = stale.
- **Decay formula**: 14-day grace period, then -0.1 per 14-day period. Uses ceiling division `(days_beyond_grace + 13) // 14` to avoid off-by-one. Separate from `compute_confidence()` (keeps citations.py pure).
- **Hysteresis**: `stale_streak` counter must reach >= 2 consecutive sweeps before demotion. Prevents single-sweep false positives.
- **Demotion preserves status**: `update_confidence()` never overwrites 'demoted' — explicit `reactivate_entry()` required.
- **Re-promotion**: When a demoted entry reappears in auto-memory with confidence >= 0.3, it's automatically reactivated.
- **Demotion atomicity**: Four-phase ordering: (1) scan files + journal intent, (2) write files, (3) update DB, (4) mark journal committed. File write happens before DB update so crash recovery can reconcile.
- **Journal crash recovery**: Sweep checks `get_unresolved_demotions()` at start. If journal says "demoted" but DB disagrees, reconciles.
- **Timezone safety**: Naive timestamps from SQLite `datetime('now')` are normalized to UTC-aware before arithmetic.
- **Path safety**: `Path.resolve()` + `is_relative_to()` for all citation paths. Symlinks dereferenced. Resolved paths stored as relative in SQLite.
- **Regex safety**: `_ABSOLUTE_PATH_RE` capped at 512 chars, `extract_citations()` caps input at 50KB.
- **Citation idempotency**: `UNIQUE(entry_hash, citation_value)` + `ON CONFLICT DO UPDATE` prevents unbounded row growth across runs.
- **Foreign keys**: `ON DELETE CASCADE` on citations and citation_checks → memory_entries.
- **CHECK constraint**: New DBs enforce `status IN ('active', 'stale', 'orphaned', 'demoted')`. Existing DBs accept the values but may not have the CHECK (acceptable since all writes go through MetadataStore methods).
- **Cold-start cap**: `--max-candidates N` (default 10) limits candidates per run, ranked by snapshot_count descending. Prevents overwhelming batch approval on first run against mature projects. Deferred entries are promoted on subsequent runs. Use `--max-candidates 0` for unlimited.

## Constraints

- SessionStart hook nudges when MEMORY.md exceeds line budget (no auto-modification)
- No MCP server (skill-only)
- `.intermem/` must be in `.gitignore` for the target project
- Python stdlib only (sqlite3 is stdlib)
