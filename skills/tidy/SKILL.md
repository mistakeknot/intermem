---
description: "Tidy memory files — extract oversized sections, detect stale counts"
---

# Memory Tidy

Structural housekeeping for auto-memory files. Extracts oversized sections from MEMORY.md into linked topic files and detects stale point-in-time counts.

## Instructions

### Running the Tidy

Find the intermem plugin install path and run the tidy subcommand:

```bash
INTERMEM_DIR="$(find ~/.claude/plugins/cache -path '*/intermem/*/pyproject.toml' -printf '%h' -quit 2>/dev/null)"
cd "$INTERMEM_DIR" && uv run python -m intermem tidy --project-dir /path/to/project --json
```

Replace `/path/to/project` with the actual project directory.

### Workflow

1. **Dry-run first** (default): Shows what would be extracted and which counts are stale
2. **Review**: Check the proposed extractions and stale counts
3. **Apply**: Run again with `--apply` to execute the extractions

```bash
# Dry-run
cd "$INTERMEM_DIR" && uv run python -m intermem tidy --project-dir /path/to/project

# Apply after review
cd "$INTERMEM_DIR" && uv run python -m intermem tidy --project-dir /path/to/project --apply
```

### Options

- `--budget N` — Maximum MEMORY.md line count (default: 120). Sections are extracted until under budget.
- `--section-threshold N` — Minimum section size to consider for extraction (default: 15 lines). Smaller sections stay inline.
- `--apply` — Execute changes. Without this flag, only shows a preview.
- `--json` — Output results as JSON for programmatic consumption.

### What It Does

**Section extraction:**
- Parses MEMORY.md into sections by `##` headings
- Identifies sections exceeding `--section-threshold` lines
- Extracts them to `<slug>.md` topic files in the same directory
- Replaces sections with links in a `## Topic Files` section
- Never touches `## Quick Reference` or `## Topic Files` (protected)
- Never clobbers existing topic files

**Stale count detection:**
- Scans for patterns like `48 plugins`, `94 tests`, `15 skills`
- For directory-countable patterns (e.g., plugins → `interverse/`), checks actual count
- Reports drift: `"48 Interverse plugins" → actual: 52 (drift: +4)`
- Non-verifiable counts are flagged with `[?]`

### When to Use

- When MEMORY.md exceeds ~120 lines (the SessionStart hook will nudge you)
- After a project grows significantly and counts become stale
- Before running `/intermem:synthesize` — a tidy source produces better promotions
