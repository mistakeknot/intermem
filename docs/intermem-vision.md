# intermem — Vision and Philosophy

**Version:** 0.1.0
**Last updated:** 2026-02-28

## What intermem Is

intermem is a memory synthesis plugin that promotes stable auto-memory facts into curated reference documents. It reads entries from a project's auto-memory files (MEMORY.md, topic memory files), scores each entry for stability and citation confidence across observation snapshots, and graduates the entries that have earned it into AGENTS.md or CLAUDE.md — the durable agent-readable docs that persist across sessions.

Graduation is not permanent by default. Promoted entries decay when citations break or when they stop appearing in auto-memory over time. Entries that remain correct get reinforced; entries that drift stale get demoted back out. The result is a living reference layer that reflects what the project actually knows, not what it once knew.

## Why This Exists

Auto-memory is noisy by design — agents write observations as they encounter them, and some of those observations turn out to be wrong, context-specific, or superseded. Without a graduation step, stale facts accumulate in the reference layer and silently degrade agent output. intermem closes this loop: it makes confidence an explicit, measurable property, and it enforces decay on entries that can no longer justify their presence. The promotion journal is the receipt — every entry in a target doc has a traceable history of how it got there.

## Design Principles

1. **Provenance over assertion.** An entry earns its place through repeated observation and valid citations, not by being written once with conviction. Stability scores are computed from content-hash history across snapshots; confidence is computed from citation validity. Both are reproducible from the state files.

2. **Decay by default.** Entries that stop appearing in auto-memory lose confidence over time via a 14-day decay schedule. This is not a cleanup heuristic — it is the primary anti-gaming mechanism. Agents cannot reinforce wrong assumptions by repeating them; they can only reinforce correct ones that continue to be observed.

3. **Atomicity as correctness.** The promotion pipeline writes files before updating the database, and the WAL-style journal tracks intent before any write. A crash leaves the system in a reconcilable state, not a corrupt one. Demotion uses the same discipline: four-phase ordering with journal crash recovery on sweep start.

4. **Hysteresis before action.** A single sweep showing stale confidence does not trigger demotion. The `stale_streak` counter requires two consecutive sweeps below threshold before demotion fires. This prevents false positives from transient citation failures or auto-memory gaps.

5. **Single-responsibility state.** Three state files serve distinct roles: `stability.jsonl` tracks content hash history, `promotion-journal.jsonl` provides atomic operation receipts, `metadata.db` stores entry provenance and demotion status. Each is independently interpretable. Deleting `metadata.db` restores Phase 0.5 behavior without corrupting the JSONL history.

## Scope

**What intermem does:**
- Scans auto-memory markdown into structured entries with stable hashes
- Scores entries for citation validity and snapshot stability
- Promotes qualifying entries into target docs with hash-based markers
- Runs periodic decay sweeps and demotes entries that no longer qualify
- Surfaces query views: by keyword, by topic section, by demotion status

**What intermem does not do:**
- Write auto-memory entries (that is the agent's job during sessions)
- Manage which target docs exist or their structure
- Hook into file saves or run automatically (no hooks — skill-invoked only)
- Depend on any external packages (Python stdlib only)

## Direction

- Phase 2B: cross-project reference tracking — entries promoted in one project that reference patterns applicable to siblings
- Confidence calibration reporting: surface entries near the promotion/demotion threshold to guide agent review
- Integration with interwatch: expose promotion and demotion events as watchable signals for dashboards
