# intermem

Memory synthesis for Claude Code.

## What This Does

Claude Code's auto-memory accumulates facts over time, but they live in a flat file that grows without bound. Some of those facts are stable and well-confirmed; others are speculative or stale. intermem graduates the stable ones into curated reference documents (AGENTS.md, CLAUDE.md) and prunes the source.

The pipeline: scan memory files → detect stability (same fact confirmed across multiple sessions via SHA-256 content hashing) → validate citations and paths → deduplicate against existing docs → present for interactive approval → promote to target document + prune from source. A WAL-style journal ensures atomicity — if the process crashes mid-promotion, it recovers cleanly.

## Installation

```bash
/plugin install intermem
```

## Usage

Run the synthesis pipeline:

```
/intermem:synthesize
```

Validate memory health without promoting:

```
/intermem:validate
```

## Architecture

```
intermem/            Python library (scan, stability, validation, dedup, promotion)
skills/              synthesize, validate
.intermem/           Per-project state (metadata.db in SQLite)
```

## Design Decisions

- Confidence scoring: base 0.5, +0.3 for valid citations, -0.4 for broken citations, +0.2 for 5+ snapshot appearances. Below 0.3 is considered stale.
- No hooks (Clavain hook budget constraint) — synthesis is always manually triggered
- No MCP server — skills are sufficient for this workflow
- `.intermem/` must be in `.gitignore` (local state, not shared)
