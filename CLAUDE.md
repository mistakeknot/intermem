# intermem

Memory synthesis plugin — graduates stable auto-memory facts to curated reference documents.

## Quick Reference

- **Plugin manifest**: `.claude-plugin/plugin.json`
- **Library**: `intermem/` (Python, run via `uv run`)
- **State directory**: `.intermem/` in target project root
- **Skill**: `/intermem:synthesize`

## Architecture

Pipeline: scan → stability → dedup → approve → promote+prune

- `intermem/scanner.py` — Parse auto-memory markdown into structured entries
- `intermem/stability.py` — Per-entry content hashing across snapshots
- `intermem/dedup.py` — Fuzzy matching against AGENTS.md/CLAUDE.md
- `intermem/promoter.py` — Write entries to target docs with marker comments
- `intermem/pruner.py` — Remove promoted entries from auto-memory
- `intermem/journal.py` — WAL-style promotion journal for atomicity

## State Files

- `.intermem/stability.jsonl` — Per-entry content hash history
- `.intermem/promotion-journal.jsonl` — Atomic promotion/prune log

## Constraints

- No hooks (Clavain hook budget)
- No MCP server (Phase 0.5 is skill-only)
- `.intermem/` must be in `.gitignore` for the target project
