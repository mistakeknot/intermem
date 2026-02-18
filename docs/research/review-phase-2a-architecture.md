# Phase 2A Architecture Review: Confidence Decay & Demotion Pipeline

**Reviewer:** Flux Architecture & Design Reviewer
**Date:** 2026-02-18
**Scope:** Phase 2A implementation (time-based decay, demotion, sweep, CLI subcommands)

## Executive Summary

**Overall Assessment:** STRONG architecture with clean module boundaries and excellent crash safety discipline. The sweep→decay→demotion pipeline is well-structured and follows existing project patterns. Two structural concerns merit attention before merge: transaction boundary granularity in demotion and NULL-safety in sweep.

**Key Strengths:**
- Module boundaries preserved: each file has clear, focused responsibility
- Transaction discipline is rigorous (single-transaction sweep, journal-first demotion)
- Backward compatibility maintained through regex-based marker detection
- Re-promotion logic correctly placed in validation pipeline (no new abstraction layer)
- Timezone normalization prevents cross-platform timestamp arithmetic bugs

**Key Risks:**
- Demotion performs per-entry database commits inside file I/O loop (atomicity gap)
- NULL `last_seen` fallback in sweep (line 234) silently resets clock for corrupt entries
- CLI subparser structure creates implicit synthesis-vs-sweep mode split

---

## 1. Module Boundaries & Responsibilities

### 1.1 Clear Ownership (PASS)

Each file maintains a single, well-defined responsibility:

- **`metadata.py`**: State mutation methods (`increment_stale_streak`, `mark_demoted`, `reactivate_entry`) and query methods (`search_entries`, `get_topics`, `get_demoted_entries`). Migration logic stays isolated in `_migrate_to_v2()`.
- **`validator.py`**: Confidence computation (`apply_decay_penalty`, `compute_confidence` separation maintained), sweep orchestration (`sweep_all_entries`), and re-promotion logic (lines 110-113).
- **`promoter.py`**: Marker generation (`make_marker`), file mutation (`promote_entries`, `demote_entries`). Hash truncation logic (8 chars) co-located with marker creation.
- **`journal.py`**: Append-only log with demotion lifecycle (`demoted` → `demotion_committed`). No business logic leakage.
- **`__main__.py`**: CLI routing only. Subcommand handlers delegate to pipeline functions without reimplementing logic.

**No god modules detected.** The `MetadataStore` class grew by 6 methods but each is single-purpose (e.g., `increment_stale_streak` does not also check thresholds).

### 1.2 Dependency Direction (PASS)

Core validation and decay logic does not depend on CLI or presentation layers:
- `apply_decay_penalty()` is pure function (no MetadataStore, no journal)
- `sweep_all_entries()` takes journal as optional param (testable without it)
- CLI subcommands import pipeline functions, not the reverse

### 1.3 Naming Consistency (PASS)

New names align with existing conventions:
- `stale_streak` (counter, not boolean) matches confidence/snapshot_count naming
- `demoted_at` timestamp follows `confidence_updated_at`, `last_seen` pattern
- `SweepResult`, `DemotionResult` match `ValidationResult`, `PromotionResult` structure
- `reactivate_entry()` clearly signals inverse of `mark_demoted()` (better than `undemote`)

---

## 2. Data Flow: Sweep → Decay → Demotion Pipeline

### 2.1 Pipeline Structure (STRONG)

The secondary pipeline is well-layered:

```
CLI: intermem sweep
  ↓
sweep_all_entries(store, root, journal)
  ├─ Crash recovery: reconcile journal with DB (lines 208-213)
  ├─ Single transaction: for all active/stale entries
  │   ├─ _revalidate_citations() → CheckResult[]
  │   ├─ compute_confidence(checks) → base
  │   ├─ apply_decay_penalty(base, last_seen) → decayed
  │   ├─ update_confidence(entry_hash, decayed)
  │   └─ if stale: increment_stale_streak, else: reset
  ├─ Commit transaction
  └─ Query for demotion_candidates (streak >= 2)
  ↓
demote_entries(candidates, store, docs, journal)
  ├─ For each doc:
  │   ├─ For each line with hash-based marker:
  │   │   ├─ journal.record_demoted()
  │   │   ├─ Remove line from file
  │   │   ├─ metadata.mark_demoted()
  │   │   └─ journal.mark_demotion_committed()
  │   └─ Write modified file
  └─ Return DemotionResult
```

**Strengths:**
- Crash recovery at entry point (sweep start) before any mutations
- Demotion candidates collected *after* sweep transaction commits (prevents dirty reads)
- Journal-first pattern: `record_demoted` before file mutation (can detect incomplete demotions)

**Concern (see §4.2):** Demotion commits to DB inside the per-line loop (line 152), not after the file write. If file write fails, DB already says "demoted" but file still has the line.

### 2.2 Decay Logic Separation (EXCELLENT)

`apply_decay_penalty()` is pure, separate from `compute_confidence()` (which stays in `citations.py`):
- **Grace period:** 14 days, no penalty
- **Penalty bands:** -0.1 per additional 14-day period, ceiling division `(days_beyond_grace + 13) // 14`
- **Clamp:** `max(0.0, min(1.0, ...))` prevents negative confidence

Test coverage confirms boundary correctness (days 14, 15, 28, 29, 42, 43 — see `test_validator.py:1325-1370`).

**Why this matters:** Keeps `citations.py` free of time-dependency injection, making snapshot-based confidence scoring testable without mocking time.

### 2.3 Hysteresis (DEMOTION_STREAK_THRESHOLD = 2) (STRONG)

Prevents single-sweep false positives:
- Entry must be stale in 2 consecutive sweeps before demotion candidacy
- `reset_stale_streak()` on recovery (lines 255, 369) prevents streak inflation from transient issues

**Edge case handled:** `update_confidence()` resets streak on stale→active transition (lines 368-369), so brief file-not-found followed by re-appearance doesn't accumulate streaks.

---

## 3. Backward Compatibility

### 3.1 Marker Detection (PASS)

Regex `r"<!--\s*intermem(?::([a-f0-9]+))?\s*-->"` matches both:
- Old: `<!-- intermem -->` (group 1 = None)
- New: `<!-- intermem:abc123de -->` (group 1 = "abc123de")

**Demotion safety (line 147):** `if marker_hash and marker_hash in short_to_full` — old markers have `marker_hash = None`, so `and` short-circuits and they're never demoted.

**Test coverage:** `test_demote_old_style_marker_not_removed` (line 1146) confirms old markers survive demotion sweeps.

### 3.2 Schema Migration (PASS)

`_migrate_to_v2()` is idempotent:
- Uses `_column_exists()` before `ALTER TABLE`
- New DBs get CHECK constraint on `status`, old DBs don't (documented as acceptable in CLAUDE.md:66)
- Default values (`stale_streak = 0`, `demoted_at = NULL`) prevent NULL crashes for existing rows

**No data loss risk:** Existing entries preserve their `status` values ("active", "stale", "orphaned") and gain zeros for new columns.

### 3.3 CLI Backward Compatibility (PASS)

No subcommand → falls through to synthesis mode (line 257):
- All existing flags (`--dry-run`, `--validate`, `--auto-approve`) stay on main parser
- Shared args (`--project-dir`, `--json`) available to all modes via `parents=[shared]`

**Subtle risk:** If a user runs `intermem --dry-run sweep`, argparse will treat `sweep` as positional arg for synthesis mode, not as subcommand. This is acceptable (no flag collision) but could confuse users expecting `--dry-run` to work with sweep. Document in CLI help or fail early if synthesis gets unexpected positional args.

---

## 4. Crash Safety & Transaction Boundaries

### 4.1 Sweep Transaction Discipline (EXCELLENT)

Single transaction wraps entire sweep (lines 215-260):
- All confidence updates, streak increments/resets within transaction
- Rollback on exception (line 259) ensures no partial updates
- Demotion candidates queried *after* commit (line 263) — prevents dirty reads

**Test coverage:** `test_sweep_single_transaction_rollback` (line 1454) confirms monkeypatch crash mid-sweep leaves DB unchanged.

### 4.2 Demotion Atomicity Gap (ARCHITECTURAL CONCERN)

**Problem:** `demote_entries()` performs per-entry DB updates inside the file I/O loop:

```python
for line in lines:
    if match and marker_hash in short_to_full:
        journal.record_demoted(full_hash, str(doc), "", content)
        metadata_store.mark_demoted(full_hash)        # ← DB commit happens here (if autocommit on)
        journal.mark_demotion_committed(full_hash)
        continue  # Remove line
new_lines.append(line)

if removed_in_doc:
    doc.write_text("".join(new_lines))  # ← File write after DB already says "demoted"
```

**Failure scenario:**
1. `mark_demoted(h1)` commits to DB
2. `mark_demotion_committed(h1)` appends to journal
3. `doc.write_text()` raises `PermissionError` (readonly file, disk full, etc.)
4. Result: DB says entry is demoted, journal says committed, but file still has the line

**Journal crash recovery won't catch this:** `get_unresolved_demotions()` only returns entries with status "demoted" (not "demotion_committed"). The sweep reconciliation (lines 208-213) checks if DB disagrees with journal, but in this scenario they agree (both say demoted) — the file is the inconsistent state.

**Recommended fix:**
- Defer `mark_demoted()` until after `doc.write_text()` succeeds
- Wrap per-doc logic in try/except, collect failures, commit DB updates only for successful file writes
- Or: collect all mutations in memory, single DB transaction at end covering all docs

**Alternative (lower risk but weaker):** Document that demotion is best-effort, and re-running sweep will re-detect stale entries. Acceptable if the consequence is benign (user manually removes leftover line), but breaks the journal-first invariant established in Phase 1.

### 4.3 Crash Recovery (STRONG)

Sweep checks `journal.get_unresolved_demotions()` at start (lines 208-213):
- If journal says "demoted" but DB status != "demoted", reconciles by calling `mark_demoted()`
- Commits recovery before starting sweep transaction (line 213)

**Lifecycle assumed:**
```
record_demoted → mark_demoted (DB) → mark_demotion_committed → file write
```

But actual order in code is:
```
record_demoted → mark_demoted → mark_demotion_committed → file write
```

Gap: if crash between `mark_demotion_committed` and file write, recovery won't fix the file (journal says committed, DB agrees, file still has line).

**Test gap:** `test_sweep_crash_recovery_from_journal` (line 1541) only tests DB/journal disagreement, not file/DB disagreement.

---

## 5. Transaction Boundaries: SQLite Connection Discipline

### 5.1 Explicit Transaction Management (PASS)

All pipeline functions use `begin_transaction()` / `commit_transaction()` / `rollback_transaction()`:
- `validate_and_filter_entries`: lines 70-118
- `sweep_all_entries`: lines 215-260
- `validate_promoted`: lines 337-402

**No implicit autocommit reliance** except in CLI subcommand handlers (lines 190, 224), which call `store.conn.commit()` after pipeline functions return. This is acceptable for top-level orchestration.

### 5.2 Connection Lifecycle (PASS)

CLI handlers close the store before exit:
- Sweep: line 192
- Query: line 234

No leaked connections detected.

### 5.3 NULL Safety (MINOR ISSUE)

Line 234 in `sweep_all_entries()`:
```python
last_seen = row["last_seen"]
if last_seen is None:
    last_seen = current_time.isoformat()
```

**Problem:** This silently treats NULL `last_seen` as "fresh" (resets the decay clock). If metadata.db gets corrupted or entries created before `last_seen` was added, they'll never decay.

**Correct behavior (debatable):**
- Option A: Treat NULL as maximally stale → `last_seen = "1970-01-01T00:00:00+00:00"` (forces immediate decay)
- Option B: Fail fast → `raise ValueError(f"Entry {entry_hash} has NULL last_seen")` (forces data migration)
- Option C (current): Silent fallback (avoids crashes but hides data quality issues)

**Recommendation:** Add a comment explaining the choice, or log a warning so operators know NULL timestamps are being reset. Test coverage exists for corrupt *string* timestamps (line 1510) but not for NULL.

---

## 6. Re-Promotion Logic Placement

### 6.1 Correct Layer (EXCELLENT)

Re-promotion lives in `validate_and_filter_entries()` (lines 110-113):
```python
if was_demoted and confidence >= STALE_THRESHOLD:
    metadata_store.reactivate_entry(entry_hash)
metadata_store.update_confidence(entry_hash, confidence)
```

**Why this is right:**
- Re-promotion is not a separate pipeline stage — it's a conditional status transition during validation
- No new abstraction needed (avoids premature generalization)
- Uses existing confidence threshold (0.3) as activation gate

### 6.2 Preserves Demotion Status (STRONG)

`update_confidence()` preserves "demoted" status (lines 346-355 in metadata.py):
```python
if old_status == "demoted":
    # Update confidence only, don't change status
    self.conn.execute("UPDATE ... SET confidence = ? WHERE ...", ...)
    return
```

**Separation of concerns:** Confidence can be updated during sweeps without accidentally re-promoting. Only `reactivate_entry()` or explicit re-validation (line 112) can change "demoted" → "active".

**Test coverage:** `test_update_confidence_preserves_demoted` (line 899) confirms high confidence doesn't auto-promote.

---

## 7. CLI Structure & Subcommand Architecture

### 7.1 Subparser Design (ACCEPTABLE, with caveats)

Subcommands (`sweep`, `query`) vs. no-subcommand (synthesis) creates implicit mode branching:
- Shared args via `parents=[shared]` avoids duplication
- Synthesis-specific args stay on main parser (backward compat)
- Subcommand-specific args on subparsers (`query --search`, etc.)

**Concern:** Argparse `dest="command"` defaults to `None` when no subcommand given, so line 169 `if args.command == "sweep"` works, but if someone later adds `parser.set_defaults(command="synthesis")`, behavior changes. Explicit `is None` checks would be safer.

**Naming consistency:** "sweep" and "query" are verbs, "synthesis" (implied default) is a noun. Consider `intermem synthesize` as explicit subcommand for consistency, with fallback for backward compat.

### 7.2 Error Handling (PASS)

CLI handlers catch `sqlite3.DatabaseError` (lines 174, 223) and exit with code 2. Missing file checks exit with code 2 (line 219). This is consistent with POSIX conventions (2 = usage error).

### 7.3 JSON Output Consistency (PASS)

All subcommands support `--json` flag with structured output:
- Sweep: entry counts, candidate list, demotion results (lines 194-204)
- Query: list of dicts (line 240)
- Synthesis: already had JSON output from Phase 1

---

## 8. Design Decisions Review

### 8.1 Hash Truncation (8 chars for markers, 16 for hashes) (PASS)

- **Collision risk:** At 1000 entries, 8 hex chars (32 bits) has ~0.01% collision probability (birthday problem). Acceptable for per-project scope.
- **Consistency:** Entry hashes stay 16 chars (stability.jsonl backward compat), markers use first 8 for brevity.
- **Determinism:** Same entry → same hash → same marker. No timestamp in hash, so re-promotion produces identical marker.

**Test coverage:** `test_promote_uses_hash_marker` (line 1071) confirms truncation logic.

### 8.2 Timezone Normalization (STRONG)

Lines 153-156 in `validator.py`:
```python
if last_seen_dt.tzinfo is None:
    last_seen_dt = last_seen_dt.replace(tzinfo=timezone.utc)
if current_time.tzinfo is None:
    current_time = current_time.replace(tzinfo=timezone.utc)
```

**Why critical:** SQLite's `datetime('now')` returns naive timestamps. Python's `datetime` arithmetic raises `TypeError` if mixing naive and aware. Normalizing both sides prevents cross-platform bugs.

**Test coverage:** `test_naive_last_seen_timestamp` (line 1377) confirms naive input doesn't crash.

### 8.3 Decay Formula (Ceiling Division) (CORRECT)

Line 163: `periods = (days_beyond_grace + 13) // 14`

This is ceiling division: `ceil(x / 14) == (x + 13) // 14`.

**Boundary verification (from tests):**
- Day 15: `(1 + 13) // 14 = 1` period → -0.1 ✓
- Day 28: `(14 + 13) // 14 = 1` period → -0.1 ✓ (off-by-one fixed)
- Day 29: `(15 + 13) // 14 = 2` periods → -0.2 ✓

**Alternative rejected:** `math.ceil(days_beyond_grace / 14)` — same result but requires import. Inline ceiling division is standard Python idiom.

---

## 9. Simplicity & YAGNI Analysis

### 9.1 No Premature Abstractions (PASS)

- **No DemotionPipeline class:** Demotion is a single function (`demote_entries`), not a multi-stage pipeline. Correct.
- **No DecayStrategy interface:** Single decay formula, no plugin system. Correct.
- **No SweepScheduler:** CLI subcommand is sufficient; cron/systemd timer can invoke it. Correct.

### 9.2 Necessary Complexity Only (PASS)

Every new field/method serves current requirements:
- `stale_streak`: Required for hysteresis (prevents single-sweep false positives)
- `demoted_at`: Audit trail for when entry was demoted (debugging, user visibility)
- `reactivate_entry()`: Explicit re-promotion (can't reuse `update_confidence` due to status preservation)
- `apply_decay_penalty()`: Time-based decay is Phase 2A core requirement

**No dead code:** All new methods tested, all tests passing.

### 9.3 Potential Over-Engineering (NONE DETECTED)

`_revalidate_citations()` (line 168) reconstructs `Citation` objects from DB rows. This is not premature — sweep must recheck file existence, so re-running `validate_citation()` is necessary. The `raw_match` approximation (line 181) is documented as such.

---

## 10. Structural Anti-Patterns

### 10.1 God Modules (NONE)

`MetadataStore` grew from 7 to 13 public methods, but:
- Each method is single-purpose (no mixed concerns)
- Query methods (`search_entries`, `get_topics`, `get_demoted_entries`) are thin wrappers over SQL
- State mutation methods (`increment_stale_streak`, `mark_demoted`, `reactivate_entry`) have clear pre/post conditions

**No leaky abstractions detected.**

### 10.2 Circular Dependencies (NONE)

Dependency graph:
```
__main__.py → validator.py, promoter.py, metadata.py, journal.py
validator.py → metadata.py, journal.py, citations.py
promoter.py → metadata.py, journal.py
metadata.py → (no inter-module dependencies)
journal.py → (no inter-module dependencies)
```

All edges point "down" toward data/utility layers. No cycles.

### 10.3 Cross-Layer Shortcuts (NONE)

CLI handlers (`__main__.py`) orchestrate pipeline functions but don't bypass abstractions:
- No direct SQL in CLI code
- No file I/O in CLI code (delegates to `demote_entries`)
- No business logic duplication between subcommands

---

## 11. Integration Risks

### 11.1 Filesystem Permissions (MINOR RISK)

`demote_entries()` reads entire file into memory (line 138), mutates lines (line 143-156), writes back (line 159). If target doc is read-only or on read-only filesystem, write fails after DB already marked demoted (see §4.2).

**Mitigation:** Check `doc.stat().st_mode` for write permission before entering loop, or wrap write in try/except and rollback DB on failure.

### 11.2 Concurrent Modifications (OUT OF SCOPE)

If two sweep processes run concurrently:
- SQLite WAL mode allows concurrent reads (sweep query at line 218)
- But `demote_entries()` file writes are not atomic across docs
- Risk: both processes demote same entry, write same file, last writer wins

**Acceptable:** Intermem is single-user tool (Claude Code plugin), not multi-user service. Document "do not run concurrent sweeps" in AGENTS.md if not already present.

### 11.3 Large Result Sets (MINOR RISK)

`sweep_all_entries()` loads all active/stale entries into memory (line 218). At 10,000 entries × ~200 bytes/row = 2MB. Acceptable for project-scale tool.

Query subcommands (`search_entries`, `get_topics`) have no LIMIT clause. At scale, `intermem query --search "the"` could return thousands of rows. Consider pagination or implicit LIMIT (e.g., 100) with `--all` flag to override.

---

## 12. Recommendations

### 12.1 Must-Fix (Before Merge)

**[ATOMICITY]** Demotion transaction boundary:
- Move `metadata_store.mark_demoted()` call outside the per-line loop
- Collect all demoted hashes in a list, update DB after all file writes succeed
- Or wrap per-doc logic in try/except, only commit DB for successful writes

**[NULL SAFETY]** NULL `last_seen` handling in sweep:
- Add comment explaining why `current_time.isoformat()` fallback is chosen
- OR: Log warning when NULL detected so operators know data was reset
- OR: Change to fail-fast if strict data quality preferred

### 12.2 Should-Fix (Before Release)

**[TEST COVERAGE]** Add test for demotion crash after DB commit but before file write:
```python
def test_demote_crash_before_file_write(tmp_path, store):
    # Monkeypatch doc.write_text to raise after mark_demoted succeeds
    # Verify journal/DB/file are inconsistent
    # Re-run sweep → should detect and reconcile
```

**[DOCUMENTATION]** Add to AGENTS.md or CLAUDE.md:
- "Do not run concurrent `intermem sweep` processes"
- "`intermem query` returns unlimited results; use grep/head for large projects"

**[CLI UX]** Consider explicit `intermem synthesize` subcommand for consistency, with fallback for backward compat.

### 12.3 Optional (Quality of Life)

**[PERFORMANCE]** `_revalidate_citations()` re-checks every citation on every sweep. For entries with many citations (10+), this could be slow. Consider caching file existence checks per sweep (dict of `{path: exists}`). Only implement if profiling shows sweep is >5s for typical projects.

**[LOGGING]** Sweep summary could include:
- "Repromoted X demoted entries" (from re-promotion branch)
- "Recovered Y incomplete demotions from journal"

**[OBSERVABILITY]** `intermem query --stats`:
- Total entries, by status
- Avg confidence by section
- Oldest `last_seen` (to detect stagnant entries)

---

## 13. Final Verdict

**Architecture Quality: 8.5/10**

**Strengths:**
- Module boundaries are clean and stable
- Transaction discipline is rigorous (single-transaction sweep)
- Backward compatibility is comprehensive (marker detection, schema migration, CLI)
- Re-promotion logic correctly placed in validation layer (no premature abstraction)
- Design decisions (decay formula, hysteresis, timezone normalization) are well-reasoned and tested

**Weaknesses:**
- Demotion atomicity gap (DB commit before file write) violates journal-first pattern
- NULL `last_seen` silent fallback hides data quality issues
- Test coverage gap: crash between DB commit and file write

**Merge Readiness:** CONDITIONAL — fix demotion transaction boundary (§12.1) before merge. NULL handling and test coverage can be addressed pre-release.

**Long-Term Maintainability:** HIGH — no architectural debt introduced. Sweep and demotion are natural extensions of existing pipeline, not parallel architectures. Future phases can add scheduling, multi-project sweep, or decay tuning without refactoring core structure.

---

## Appendix: Boundary Verification Checklist

| Concern | File | Status |
|---------|------|--------|
| Sweep uses single transaction | validator.py:215-260 | ✓ PASS |
| Demotion preserves status | metadata.py:346-355 | ✓ PASS |
| Re-promotion in validation layer | validator.py:110-113 | ✓ PASS |
| Old markers not auto-demoted | promoter.py:147 | ✓ PASS |
| Crash recovery at sweep start | validator.py:208-213 | ✓ PASS |
| Decay formula correct (ceiling) | validator.py:163 | ✓ PASS |
| Timezone normalization | validator.py:153-156 | ✓ PASS |
| Schema migration idempotent | metadata.py:319-335 | ✓ PASS |
| CLI backward compatible | __main__.py:257 | ✓ PASS |
| **Demotion atomicity** | **promoter.py:143-159** | **⚠ FAIL** |
| NULL last_seen safety | validator.py:234 | ⚠ MINOR |

**Legend:**
- ✓ PASS: Correct implementation, no action required
- ⚠ FAIL: Must fix before merge (architectural correctness issue)
- ⚠ MINOR: Should fix before release (data quality/observability issue)
