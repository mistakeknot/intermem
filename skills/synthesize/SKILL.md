# Memory Synthesis

Scan auto-memory for the current project, identify stable facts, and promote them to AGENTS.md/CLAUDE.md with user approval.

## Instructions

Run the intermem synthesis pipeline using the Python library:

1. **Scan**: Read auto-memory files from the project's memory directory
2. **Stability**: Check each entry against `.intermem/stability.jsonl` — only promote entries stable across 3+ snapshots
3. **Dedup**: Compare stable candidates against existing AGENTS.md/CLAUDE.md content
4. **Approve**: Present candidates to the user with batch approval (numbered list)
5. **Promote+Prune**: Write approved entries to target docs, prune from auto-memory

### Running the Pipeline

```bash
cd plugins/intermem && uv run python -m intermem --project-dir "$(pwd)"
```

### First Run Behavior

On first run (no `.intermem/stability.jsonl`), the scanner records a baseline snapshot. All entries are scored "recent" — none are promoted. The user must run synthesis again after a few more sessions to build history.

Display: "Building baseline — recorded N entries. Run again after your next few sessions to identify stable facts."

### Batch Approval UX

Present candidates as a numbered list grouped by target document:

```
Candidates for AGENTS.md:
  1. [stable] Oracle CLI requires DISPLAY=:99 (section: Cross-Cutting Lessons)
  2. [stable] Never use > file redirect with oracle (section: Cross-Cutting Lessons)
  3. [similar to existing] Git credential lock fix... (section: Cross-Cutting Lessons)

Candidates for CLAUDE.md:
  4. [stable] Always use uv run for Python deps (section: Tool Usage)

Enter selections (e.g., "approve 1,2,4 / reject 3" or "all"):
```

### Routing Rule

- Structural/architectural facts → AGENTS.md
- Behavioral preferences, tool usage → CLAUDE.md
- If uncertain, default to AGENTS.md (shared with all agents)
