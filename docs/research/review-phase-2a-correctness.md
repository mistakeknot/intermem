# Intermem Phase 2A Correctness Review

**Reviewer**: Julik (Flux-drive Correctness Reviewer)
**Date**: 2026-02-18
**Scope**: Time-based decay, sweep pipeline, demotion, and re-promotion logic

---

## Executive Summary

Phase 2A adds time-based confidence decay, stale entry demotion, and re-promotion logic to intermem. The implementation demonstrates strong correctness fundamentals with proper transaction boundaries, timezone handling, and crash recovery. However, **three high-severity issues** require immediate correction before merge:

1. **CRITICAL: Demotion ordering violates crash-safety contract** (journal→file→DB creates unrecoverable state)
2. **HIGH: Sweep intra-transaction read-after-write introduces phantom stale streak** (uncommitted writes read back as committed state)
3. **MEDIUM: Hash collision window is wider than stated** (8-char markers at scale)

The decay formula, timezone normalization, and re-promotion logic are correct. Transaction rollback for sweep is properly implemented. With the three issues addressed, this phase will be production-ready.

---

## Issue 1: CRITICAL — Demotion Ordering Violates Crash-Safety

### Location
`intermem/promoter.py:150-152` (demote_entries)

### Finding

The demotion sequence is:

```python
journal.record_demoted(full_hash, str(doc), "", content)
metadata_store.mark_demoted(full_hash)
journal.mark_demotion_committed(full_hash)
```

This creates **three failure windows** that leave persistent state corrupted:

1. **Crash between journal write and file write**: Journal says "demoted", file still contains entry, DB says "active". Sweep recovery marks DB as "demoted" but entry remains in file forever (orphaned promoted fact).
2. **Crash between file write and DB write**: File is modified, journal says "demoted", DB says "active". Sweep recovery marks DB as "demoted", but the entry was already removed from the file — **no way to restore it**. User loses a potentially valid fact with no undo path.
3. **Crash between DB write and journal commit**: DB says "demoted", journal still says "unresolved". Sweep recovery redundantly calls `mark_demoted` (idempotent, safe).

### Failure Narrative: Data Loss at 3 AM

1. User runs `intermem sweep` on a project with 50 entries.
2. Entry `abc123` is stale (stale_streak >= 2), marked for demotion.
3. Demotion starts: journal writes "demoted", file write removes line from AGENTS.md, **process killed (OOM, Ctrl+C, container restart)**.
4. On next run, sweep recovery reads journal, sees "demoted" status, calls `metadata_store.mark_demoted("abc123")`.
5. **Result**: DB says "demoted", file has no trace of the entry. If the entry was temporarily stale due to broken symlink or renamed file, user has no way to recover it. The content is **only** in the journal (which is append-only, not designed for content recovery).

### Root Cause

The current order prioritizes journal-first for "crash recovery detection" but executes **non-idempotent file mutation** before the authoritative DB update. File writes are not transactional with SQLite, so crash safety requires **idempotent operations only** or **compensating reads**.

### Correct Ordering (Two Options)

#### Option A: DB-first (recommended)
```python
# 1. Mark in DB first (idempotent, transactional)
metadata_store.mark_demoted(full_hash)

# 2. Write to file (idempotent if we check marker existence)
if removed_in_doc:
    doc.write_text("".join(new_lines), encoding="utf-8")

# 3. Record in journal AFTER both succeed (audit trail, not recovery mechanism)
journal.record_demoted(full_hash, str(doc), "", content)
journal.mark_demotion_committed(full_hash)
```

**Crash recovery**: Sweep queries DB for `status = 'demoted'`, scans target docs for markers with those hashes, removes any still present. Journal becomes audit-only.

#### Option B: Two-phase with compensation
```python
# Phase 1: Record intent
journal.record_demoted(full_hash, str(doc), "", content)

# Phase 2: Execute (idempotent file scan)
# (file write as-is)

# Phase 3: Commit to DB
metadata_store.mark_demoted(full_hash)
journal.mark_demotion_committed(full_hash)
```

**Crash recovery**: Sweep checks journal for "demoted" (not yet committed). For each, check if marker still exists in file. If yes, remove and mark DB. If no, mark DB only (file write already completed, idempotent).

**Recommendation**: Option A. Simpler invariant (DB is source of truth), and file scan is cheap.

---

## Issue 2: HIGH — Sweep Intra-Transaction Read Introduces Phantom Stale Streak

### Location
`intermem/validator.py:247-252` (sweep_all_entries loop)

### Finding

Within the sweep transaction, the code does:

```python
metadata_store.increment_stale_streak(entry_hash)
row_updated = metadata_store.get_entry(entry_hash)
if row_updated and row_updated["stale_streak"] >= DEMOTION_STREAK_THRESHOLD:
    entries_marked += 1
```

This **reads back uncommitted data within the same transaction**. While SQLite allows this (default isolation is DEFERRED), it creates two problems:

1. **No isolation failure**, but **misleading semantics**: The read happens inside the transaction, so it's reading the incremented value. However, if the transaction rolls back (line 259), the increment never happened, but `entries_marked` already counted it. The return value `entries_marked_for_demotion` becomes a **phantom metric** — it claims entries were marked, but the DB state doesn't reflect it.

2. **Race with external writes**: If another process (concurrent sweep? unlikely but possible with long-running sweeps) writes to `memory_entries` between the `increment_stale_streak` and `get_entry`, SQLite's DEFERRED transaction **may not see the other write** until commit, causing stale read. This is low-probability (intermem is single-threaded in practice), but the pattern is **not defensively correct**.

### Failure Narrative: Misleading Metrics on Rollback

1. Sweep starts, processes 100 entries, 10 of them stale.
2. For each stale entry, `increment_stale_streak` writes, `get_entry` reads back the incremented value, `entries_marked` increments to 10.
3. On the 95th entry, citation validation crashes (filesystem issue, permission denied on a symlink target).
4. Exception raised, `rollback_transaction()` called (line 259).
5. **Sweep returns**: `entries_marked_for_demotion = 10` (from the count), but DB state is rolled back — no entries actually have incremented stale_streak.
6. CLI prints "10 entries marked for demotion", but next sweep starts from scratch. User thinks something is wrong with the sweep logic.

### Correct Approach

**Avoid read-after-write within the transaction for metrics**. Instead, **count stale entries and decide after commit** OR **read the updated values after commit**.

#### Fix Option 1: Compute demotion candidates after commit
```python
for row in entries:
    entry_hash = row["entry_hash"]
    # ... (confidence calculation as-is)

    if decayed_confidence < STALE_THRESHOLD:
        metadata_store.increment_stale_streak(entry_hash)
        # Don't read back or count here
    else:
        metadata_store.reset_stale_streak(entry_hash)

metadata_store.commit_transaction()  # Transaction ends here

# NOW read committed state for metrics
demotion_rows = metadata_store.conn.execute(
    "SELECT entry_hash FROM memory_entries WHERE stale_streak >= ?",
    (DEMOTION_STREAK_THRESHOLD,),
).fetchall()
entries_marked = len(demotion_rows)
```

This is **already done** at line 263-268 for `demotion_candidates`, but `entries_marked` is computed inside the transaction (line 253). **Drop the intra-transaction count** and compute it from the final query.

#### Fix Option 2: Use transactional counter (if metrics must be precise)
```python
# Track increments during transaction
stale_increments = []
for row in entries:
    if decayed_confidence < STALE_THRESHOLD:
        metadata_store.increment_stale_streak(entry_hash)
        stale_increments.append(entry_hash)
    else:
        metadata_store.reset_stale_streak(entry_hash)

metadata_store.commit_transaction()

# Count how many of the incremented entries are now at threshold
entries_marked = sum(
    1 for h in stale_increments
    if metadata_store.get_entry(h)["stale_streak"] >= DEMOTION_STREAK_THRESHOLD
)
```

**Recommendation**: Fix Option 1. Drop `entries_marked` from the loop, compute it from the final query. Current code already has the correct post-commit query (line 263-268), just use that count.

---

## Issue 3: MEDIUM — Hash Collision Window Wider Than Stated

### Location
`intermem/promoter.py:130` (short_to_full mapping), CLAUDE.md line 47 (design decision)

### Finding

The design doc states:

> Hash length: SHA-256 truncated to 16 hex chars. Collision risk negligible at <1000 entries per project.

But **markers use 8 hex chars** (line 505: `entry_hash[:8]`), while the **entry hash stored in DB is 16 chars**. This creates a **two-tier collision risk**:

1. **Entry-level collisions** (16 chars): Negligible at <1000 entries. SHA-256 truncated to 64 bits gives ~2^32 entries before 50% collision probability (birthday paradox). Safe.

2. **Marker-level collisions** (8 chars): 32 bits, ~2^16 entries (65k) before 50% collision. At 1000 entries, collision probability is ~1.5% (not negligible). At 100 entries, ~0.15% (acceptable but not "negligible").

### Failure Narrative: False Demotion at Scale

1. Project grows to 500 promoted entries across AGENTS.md, CLAUDE.md, and topic-specific docs.
2. Two entries hash to `abc12345...` and `abc123ffff...` (first 8 chars match: `abc12345`).
3. Entry 1 becomes stale, marked for demotion. Sweep calls `demote_entries(["abc12345ffff..."])`.
4. Demotion scans files, finds marker `<!-- intermem:abc12345 -->` for **Entry 2** (which is healthy).
5. **Entry 2 is removed from AGENTS.md**, DB marks it as "demoted". User loses a valid fact.

### Collision Probability Math

For 8 hex chars (32 bits):
- 100 entries: ~0.15% chance of collision
- 500 entries: ~3% chance
- 1000 entries: ~12% chance
- 10,000 entries: ~95% chance (certain collision)

For 16 hex chars (64 bits):
- 1000 entries: ~0.00002% (truly negligible)
- 10,000 entries: ~0.02%
- 100,000 entries: ~2%

### Fix: Extend Marker Hash to 12 Chars

**12 hex chars = 48 bits**, collision probability:
- 1000 entries: ~0.0002% (negligible)
- 10,000 entries: ~0.2% (acceptable)

Change line 505:
```python
return f"<!-- intermem:{entry_hash[:12]} -->"
```

Update `short_to_full` construction (line 130):
```python
short_to_full = {h[:12]: h for h in entry_hashes}
```

Update regex (line 500):
```python
_INTERMEM_MARKER_RE = re.compile(r"<!--\s*intermem(?::([a-f0-9]{8,16}))?\s*-->")
```

This maintains backward compatibility (8-char markers still match, never demoted because they're old-style) while future markers use 12 chars.

**Alternative**: Use full 16-char hash in markers. Slightly longer comments, but zero collision risk at any scale.

---

## Issue 4: MEDIUM — Crash Recovery Reconciliation is Incomplete

### Location
`intermem/validator.py:208-213` (sweep crash recovery)

### Finding

Crash recovery only reconciles **journal→DB direction**:

```python
for jentry in journal.get_unresolved_demotions():
    row = metadata_store.get_entry(jentry.entry_hash)
    if row and row["status"] != "demoted":
        metadata_store.mark_demoted(jentry.entry_hash)
```

But it **does not check file→DB direction**. If the crash happened after file write but before DB write (Issue 1, window 2), the entry is missing from the file but the journal says "demoted" (unresolved). Recovery marks DB as "demoted", but **never verifies the file state**.

### Two Missing Checks

1. **File still has marker**: If journal says "demoted" but file still contains the marker, recovery should remove it (complete the demotion).
2. **File already clean**: If journal says "demoted" and file is already clean, recovery is correct (no-op on DB is fine, just mark journal committed).

Currently, recovery assumes **file write always succeeded if journal was written**, which is false if the crash happened between journal and file (Issue 1, window 1).

### Correct Recovery Logic

```python
for jentry in journal.get_unresolved_demotions():
    row = metadata_store.get_entry(jentry.entry_hash)

    # Step 1: Reconcile DB
    if row and row["status"] != "demoted":
        metadata_store.mark_demoted(jentry.entry_hash)

    # Step 2: Reconcile files (scan for marker, remove if present)
    target_file = Path(jentry.target_file)
    if target_file.exists():
        text = target_file.read_text(encoding="utf-8")
        marker_pattern = f"<!-- intermem:{jentry.entry_hash[:8]} -->"
        if marker_pattern in text:
            # Marker still present, complete the file write
            lines = text.splitlines(keepends=True)
            new_lines = [line for line in lines if marker_pattern not in line]
            target_file.write_text("".join(new_lines), encoding="utf-8")

    # Step 3: Mark journal committed
    journal.mark_demotion_committed(jentry.entry_hash)

metadata_store.conn.commit()
```

This makes recovery **idempotent** for all three failure windows.

---

## Correctness Validation: What's Right

### 1. Decay Formula (Correct)

**Claim**: 14-day grace, then -0.1 per 14-day period. Ceiling division avoids off-by-one.

**Verification**:
```python
days_beyond_grace = days_since - 14
periods = (days_beyond_grace + 13) // 14
```

- Day 14: `days_since = 14`, `days_beyond_grace = 0`, `periods = 13 // 14 = 0` → no penalty ✓
- Day 15: `days_beyond_grace = 1`, `periods = 14 // 14 = 1` → -0.1 ✓
- Day 28: `days_beyond_grace = 14`, `periods = 27 // 14 = 1` → -0.1 ✓
- Day 29: `days_beyond_grace = 15`, `periods = 28 // 14 = 2` → -0.2 ✓
- Day 42: `days_beyond_grace = 28`, `periods = 41 // 14 = 2` → -0.2 ✓
- Day 43: `days_beyond_grace = 29`, `periods = 42 // 14 = 3` → -0.3 ✓

**Ceiling division formula**: `(n + k - 1) // k` for ceiling of `n/k`. Here, `k=14`, so `(days_beyond_grace + 13) // 14` is correct.

**Test coverage**: `test_validator.py:1330-1395` exhaustively covers boundary cases. All pass. ✓

---

### 2. Transaction Safety (Correct Except for Issue 2)

**Sweep transaction**:
```python
metadata_store.begin_transaction()
try:
    # ... all confidence/streak updates ...
    metadata_store.commit_transaction()
except Exception:
    metadata_store.rollback_transaction()
    raise
```

**Rollback test**: `test_validator.py:1455-1485` (test_sweep_single_transaction_rollback). Simulates crash after processing first entry, verifies no partial updates survive. ✓

**Idempotency**: All DB writes (`increment_stale_streak`, `reset_stale_streak`, `mark_demoted`) are `UPDATE` statements with WHERE clauses, safe to re-run. ✓

**Problem**: Issue 2 (intra-transaction read for metrics) makes rollback invisible to the caller, but DB state is correct. Fix by removing the intra-transaction count.

---

### 3. Timezone Handling (Correct)

**Code**:
```python
if last_seen_dt.tzinfo is None:
    last_seen_dt = last_seen_dt.replace(tzinfo=timezone.utc)
if current_time.tzinfo is None:
    current_time = current_time.replace(tzinfo=timezone.utc)
```

**Rationale**: SQLite `datetime('now')` produces naive timestamps (no TZ info). Python `datetime.now(timezone.utc)` produces aware timestamps. Without normalization, `datetime` arithmetic raises `TypeError: can't subtract offset-naive and offset-aware datetimes`.

**Test coverage**: `test_validator.py:1383-1390` (test_naive_last_seen_timestamp, test_aware_last_seen_timestamp). Both pass. ✓

**Edge case**: Corrupt timestamp (non-ISO8601 string) returns 0.0 confidence (line 150), treating entry as maximally stale. This is safe (conservative) and tested (test_corrupt_timestamp_returns_zero). ✓

---

### 4. Re-Promotion Logic (Correct)

**Code** (`validator.py:100-104`):
```python
was_demoted = row is not None and row["status"] == "demoted"
confidence = compute_confidence(0.5, checks, snapshot_count)

if was_demoted and confidence >= STALE_THRESHOLD:
    metadata_store.reactivate_entry(entry_hash)
metadata_store.update_confidence(entry_hash, confidence)
```

**Invariant**: Demoted entry with good confidence (>= 0.3) is reactivated before confidence update. `update_confidence` preserves "demoted" status (lines 346-351), so reactivation must happen first.

**Test coverage**: `test_validator.py:1215-1236` (test_demoted_entry_repromotion). Entry marked demoted, re-enters pipeline with valid citation, status becomes "active". ✓

**Hysteresis check**: Entry with broken citations stays demoted (test_demoted_entry_stays_demoted_if_stale). ✓

**Full lifecycle test**: `test_full_lifecycle_decay_to_repromotion` validates the round-trip (promote → demote → reappear → re-promote). ✓

---

### 5. Demotion Preserves Status (Correct)

**Code** (`metadata.py:336-351`):
```python
old_status = old_row["status"] if old_row else None

if old_status == "demoted":
    # Update confidence but keep status
    self.conn.execute(
        "UPDATE memory_entries SET confidence = ?, confidence_updated_at = ? "
        "WHERE entry_hash = ?",
        (confidence, now, entry_hash),
    )
    return

# Otherwise, derive status from confidence
status = "active" if confidence >= STALE_THRESHOLD else "stale"
```

**Invariant**: Once demoted, only `reactivate_entry()` can change status back to "active". `update_confidence` never overwrites "demoted".

**Test coverage**: `test_metadata.py:899-909` (test_update_confidence_preserves_demoted). Entry marked demoted, then updated with good confidence (0.9), status remains "demoted". ✓

---

### 6. Stale Streak Hysteresis (Correct)

**Threshold**: `DEMOTION_STREAK_THRESHOLD = 2` (two consecutive stale sweeps before demotion).

**Logic**:
```python
if decayed_confidence < STALE_THRESHOLD:
    metadata_store.increment_stale_streak(entry_hash)
    # Check if threshold reached
else:
    metadata_store.reset_stale_streak(entry_hash)
```

**Reset on recovery**: `update_confidence` resets streak when entry transitions from stale→active (lines 368-369). ✓

**Test coverage**: `test_metadata.py:846-867` (increment, reset), `test_validator.py:1429-1452` (reset during sweep). ✓

---

### 7. Migration Safety (Correct)

**Code** (`metadata.py:319-335`):
```python
def _migrate_to_v2(self) -> None:
    if not self._column_exists("memory_entries", "stale_streak"):
        self.conn.execute("ALTER TABLE ... ADD COLUMN stale_streak INTEGER NOT NULL DEFAULT 0")
    if not self._column_exists("memory_entries", "demoted_at"):
        self.conn.execute("ALTER TABLE ... ADD COLUMN demoted_at TEXT")
    self.conn.commit()
```

**Idempotency**: `_column_exists` check prevents re-running `ALTER TABLE` on already-migrated DBs (would raise SQLite error). ✓

**CHECK constraint note**: Line 318 documents that old DBs don't have `CHECK(status IN ...)` enforcement, but all writes go through MetadataStore methods (no raw SQL), so this is safe. ✓

**Test coverage**: `test_metadata.py:995-1010` (idempotent migration, new entry defaults). ✓

---

## Race Condition Analysis

### 1. Concurrent Sweep Runs (Low Risk)

**Scenario**: Two `intermem sweep` processes run simultaneously on the same `.intermem/` dir.

**SQLite locking**: Default mode is DEFERRED transaction. First sweep acquires write lock on BEGIN IMMEDIATE (explicit, or on first write). Second sweep blocks until commit.

**Risk**: If neither uses BEGIN IMMEDIATE, both can start DEFERRED transactions, then deadlock on the first write. SQLite returns SQLITE_BUSY, one process retries.

**Current code**: `metadata_store.begin_transaction()` calls `BEGIN` (line in metadata.py not shown, but implicit in code). Should be `BEGIN IMMEDIATE` for write-heavy transactions.

**Fix**: Add to MetadataStore:
```python
def begin_transaction(self):
    self.conn.execute("BEGIN IMMEDIATE")
```

This guarantees exclusive write lock from the start, avoiding retries.

### 2. Synthesis + Sweep Overlap (Low Risk)

**Scenario**: User runs `intermem` (synthesis) in one terminal, `intermem sweep` in another.

**Conflict**: Synthesis reads `.intermem/metadata.db` (reads entries), sweep writes (updates confidence, stale_streak). SQLite allows concurrent read+write with WAL mode (default is DELETE mode, no concurrent write).

**Current code**: No explicit WAL mode pragma. DELETE mode means write lock blocks all readers.

**Fix**: Enable WAL mode for concurrent reads:
```python
self.conn.execute("PRAGMA journal_mode = WAL")
```

This is safe for intermem (single project, local filesystem, no NFS).

### 3. File Write Races (Non-Issue)

**Scenario**: Two processes demote different entries from the same AGENTS.md file.

**OS-level race**: Both read file, filter different lines, write back. Last write wins, one demotion is lost.

**Intermem context**: Single-user tool, no parallel execution expected. If needed, add file-level locking (fcntl.flock on POSIX).

**Current risk**: Negligible (no user workflow involves parallel sweep).

---

## Null Safety Validation

### 1. Null `last_seen` Handling (Correct)

**Code** (`validator.py:233-235`):
```python
last_seen = row["last_seen"]
if last_seen is None:
    last_seen = current_time.isoformat()
```

**Invariant**: `last_seen` is set on `upsert_entry` with `datetime('now')`, so NULL should never occur. Defense-in-depth: if NULL somehow appears (manual DB edit, old schema), treat as "just now" → no decay penalty. ✓

**Test**: Not explicitly tested, but `test_sweep_corrupt_last_seen` covers invalid string. Add test for NULL case:
```python
def test_sweep_null_last_seen(tmp_path, store):
    store.upsert_entry("h1", "fact", "sec", "f.md")
    store.conn.execute("UPDATE memory_entries SET last_seen = NULL WHERE entry_hash = 'h1'")
    store.conn.commit()

    result = sweep_all_entries(store, tmp_path)
    assert result.entries_swept == 1
    assert result.entries_decayed == 0  # NULL treated as current time
```

**Recommendation**: Add this test for completeness.

### 2. Null `demoted_at` Handling (Correct)

**Schema**: `demoted_at TEXT` (nullable by default). Only set when `mark_demoted` is called.

**Read logic**: `get_demoted_entries()` returns all rows with `status = 'demoted'`, including `demoted_at`. NULL is valid (means entry was demoted before Phase 2A tracking).

**No arithmetic on `demoted_at`**: It's only used for display (CLI output). Safe. ✓

---

## Test Coverage Gaps

### 1. Missing: Hash Collision Test

**Gap**: No test verifies behavior when two entries hash to the same 8-char marker prefix.

**Add**:
```python
def test_demote_hash_collision(tmp_path, store):
    """Two entries with same 8-char prefix: only target entry demoted."""
    # Manually craft two entries with controlled hashes (or monkeypatch hash_entry)
    # Promote both, mark one for demotion, verify only that one is removed
    pass
```

**Workaround**: Use 12-char markers (Issue 3 fix), reducing collision probability to negligible.

### 2. Missing: Concurrent Sweep Test

**Gap**: No test for SQLITE_BUSY or lock contention when two sweeps run.

**Add**: Use `multiprocessing` to spawn two sweep processes, verify one succeeds and the other either blocks or fails gracefully.

**Priority**: Low (single-user tool, low risk).

### 3. Missing: Null `last_seen` Test

**Gap**: Covered above (Null Safety Validation). Add test.

---

## Performance Considerations (Out of Scope, but Noted)

### 1. Sweep is O(N) in Memory

**Code** (`validator.py:218-220`):
```python
entries = metadata_store.conn.execute(
    "SELECT * FROM memory_entries WHERE status IN ('active', 'stale')"
).fetchall()
```

**`.fetchall()` loads entire result set into memory**. For 10k entries at ~1KB each (citations), this is 10MB. Acceptable for expected scale (<1000 entries).

**At scale**: Use cursor iteration (`for row in cursor:`) to stream rows.

### 2. Demotion is O(N*M) for Files

**Code** (`promoter.py:135-156`): For each target doc, scan all lines, regex match each marker.

**Complexity**: N = number of files (typically 2-5), M = lines per file (100-500). Total ~1000 line scans. Negligible.

**Optimization**: Pre-compile regex once (already done at line 500). ✓

---

## Summary of Required Fixes

| Issue | Severity | Lines Affected | Fix Effort | Blocks Merge? |
|-------|----------|----------------|------------|---------------|
| 1. Demotion ordering | CRITICAL | promoter.py:150-152 | 30 min | YES |
| 2. Intra-transaction read | HIGH | validator.py:247-253 | 10 min | YES |
| 3. Hash collision (8→12 chars) | MEDIUM | promoter.py:505, 130 | 15 min | NO (low probability at <1000 entries) |
| 4. Crash recovery file check | MEDIUM | validator.py:208-213 | 45 min | NO (depends on fix 1) |

**Recommendation**: Fix Issues 1 and 2 before merge. Issue 3 is a scaling concern (address in Phase 2B or before 1.0). Issue 4 becomes unnecessary if Issue 1 is fixed correctly (DB-first ordering makes file state always recoverable from DB).

---

## Correctness Invariants (Validated)

1. **Decay monotonicity**: For fixed confidence and increasing time, `apply_decay_penalty` never increases confidence. ✓
2. **Hysteresis**: Entry must be stale for >= 2 consecutive sweeps before demotion. ✓
3. **Re-promotion threshold**: Demoted entry with confidence >= STALE_THRESHOLD (0.3) is reactivated. ✓
4. **Transaction atomicity**: Sweep updates are all-or-nothing (rollback test passes). ✓
5. **Demotion idempotency**: `mark_demoted` can be called multiple times (UPDATE with WHERE, no INSERT). ✓
6. **Timezone normalization**: Naive and aware datetimes handled without TypeError. ✓
7. **Status preservation**: `update_confidence` never overwrites "demoted" status. ✓

---

## Final Recommendation

**DO NOT MERGE** until Issues 1 and 2 are resolved. These are **data-integrity failures** that can cause permanent loss of user content (Issue 1) or misleading metrics that erode trust in the tool (Issue 2).

Once fixed:
- Extend test coverage for hash collisions (even if using 12-char markers).
- Document crash recovery behavior in CLAUDE.md under "Journal crash recovery".
- Consider adding `BEGIN IMMEDIATE` for sweep transactions to avoid lock contention at scale.

The decay formula, re-promotion logic, and transaction boundaries are **production-grade** — excellent work on those. The demotion pipeline needs one more iteration to match that quality bar.

---

**End of Review**
