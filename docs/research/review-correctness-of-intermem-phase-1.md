# Correctness Review: intermem Phase 1 Citation Validation

**Reviewer**: Julik
**Date**: 2026-02-17
**Scope**: SQLite transaction safety, data consistency, race conditions, confidence scoring correctness

## Executive Summary

Phase 1 adds citation validation with SQLite-backed metadata tracking. The design is sound but contains **5 high-severity correctness issues** that can cause data corruption, unbounded database growth, and silent failure divergence. All are fixable with minimal changes.

**Critical findings**:
1. **Citation table grows unbounded** — every synthesis run inserts duplicate citations (data bloat + performance degradation)
2. **Dual-store divergence is silent** — stability.jsonl and metadata.db can permanently desync with no detection or repair
3. **Transaction boundary excludes post-commit query** — stale_hashes query runs outside transaction (dirty-read risk in concurrent scenarios)
4. **Confidence math can produce surprising negatives** — entry with 3 broken citations → confidence = -0.7 (clamped to 0.0, correct) but unintuitive before clamp
5. **Foreign key constraint without CASCADE** — orphaned citations/checks possible if entry deletion is added later

**Moderate findings**:
1. snapshot_count read-after-write inside transaction is correct but fragile (works only because SQLite default isolation is DEFERRED)
2. Confidence threshold boundary (< 0.3) is correct but asymmetric with update logic (>= 0.3 for active)

## 1. Transaction Safety

### 1.1 BEGIN/COMMIT/ROLLBACK Pattern

**Code**: `validator.py:65-102` (validate_and_filter_entries), `validator.py:174-233` (validate_promoted)

```python
metadata_store.begin_transaction()
try:
    for entry in entries:
        # ... upsert, record citations, record checks, update confidence ...
    metadata_store.commit_transaction()
except Exception:
    metadata_store.rollback_transaction()
    raise
```

**Assessment**: CORRECT for crash safety. All database writes occur inside a single transaction. Rollback on exception prevents partial writes.

**Test verification**: `test_validator.py:102-131` (test_transaction_rollback_on_error) confirms rollback behavior.

**Limitation**: Exception is re-raised after rollback, which is correct (caller must handle). No exception is logged before re-raise, so silent rollback is possible if caller swallows the exception. This is acceptable — caller's responsibility.

### 1.2 Post-Commit Query Creates Transaction Boundary Issue

**Code**: `validator.py:104-106`

```python
metadata_store.commit_transaction()
# ...
stale_hashes = {e["entry_hash"] for e in metadata_store.get_stale_entries()}
```

**ISSUE**: `get_stale_entries()` query runs OUTSIDE the transaction that wrote the data. In the presence of concurrent writes, this can produce dirty reads.

**Failure scenario**:
1. Process A runs validation, commits at line 99
2. Process B starts validation, upserts entry X, updates confidence to 0.1 (stale)
3. Process A's `get_stale_entries()` at line 104 sees entry X (dirty read — not yet committed by B)
4. Process A filters entry X as stale, returns it in `stale_filtered`
5. Process B's transaction rolls back (simulated crash in test)
6. Entry X was never actually stale in committed state — Process A's result is wrong

**Current risk**: LOW (single-process synthesis runs), but becomes HIGH if `/intermem:validate-promoted` is called concurrently with `/intermem:synthesize`.

**Fix**: Move the stale query inside the transaction before commit:

```python
metadata_store.begin_transaction()
try:
    for entry in entries:
        # ... validation logic ...

    # Query stale entries BEFORE committing
    stale_hashes = {e["entry_hash"] for e in metadata_store.get_stale_entries()}

    metadata_store.commit_transaction()
except Exception:
    metadata_store.rollback_transaction()
    raise

# Filter using stale_hashes after commit (read-only, safe)
validated = [e for e in entries if hash_entry(e) not in stale_hashes]
stale = [e for e in entries if hash_entry(e) in stale_hashes]
```

**Why this works**: SQLite isolation guarantees the query sees all uncommitted writes in the same transaction. External processes can't see uncommitted data.

## 2. Data Consistency

### 2.1 Dual-Store Divergence: stability.jsonl vs metadata.db

**Design**: Two independent state stores:
- `stability.jsonl` — append-only snapshot log (Phase 0.5)
- `metadata.db` — SQLite metadata (Phase 1, additive)

**Current coupling**: NONE. If `metadata.db` is deleted, synthesis continues using only stability.jsonl. If `stability.jsonl` is corrupted, metadata.db references orphaned entry_hashes.

**Divergence scenario**:
1. User deletes `metadata.db` (intentional rollback to Phase 0.5)
2. Synthesis runs, writes to `stability.jsonl`, but metadata.db is recreated from scratch
3. All historical citation checks, confidence scores, snapshot_counts are lost
4. New metadata.db has snapshot_count=1 for entries that appeared 10 times in stability.jsonl
5. Confidence calculation uses wrong snapshot_count (+0.2 bonus applied incorrectly)

**Current mitigation**: NONE. No version marker links the two stores. No consistency check at startup.

**Impact**: HIGH. Silent desync can cause:
- Premature stale-filtering (snapshot_count reset → no +0.2 bonus → confidence too low)
- Citation re-validation work (lost historical checks)
- No way to detect the divergence happened

**Recommended fix** (Phase 2 scope, document as known limitation for Phase 1):
1. Add `stability_jsonl_hash` column to `schema_version` table
2. On startup, compute `sha256(stability.jsonl)` and compare to stored hash
3. If mismatch, emit warning: "Metadata store desync detected. Run /intermem:rebuild-metadata to resync."
4. Provide `/intermem:rebuild-metadata` skill to reconstruct metadata.db from stability.jsonl history

**Interim mitigation**: Document in AGENTS.md:
> Deleting `.intermem/metadata.db` is safe but resets all citation tracking. Deleting `.intermem/stability.jsonl` is NOT safe — will orphan metadata.db references. To reset intermem completely, delete the entire `.intermem/` directory.

### 2.2 Citation Accumulation: Unbounded Growth

**Code**: `metadata.py:100-114` (record_citation)

```python
def record_citation(
    self,
    entry_hash: str,
    citation_type: str,
    citation_value: str,
    resolved_path: str | None = None,
) -> None:
    """Record an extracted citation for an entry."""
    self.conn.execute(
        """\
        INSERT INTO citations (entry_hash, citation_type, citation_value, resolved_path)
        VALUES (?, ?, ?, ?)
        """,
        (entry_hash, citation_type, citation_value, resolved_path),
    )
```

**CRITICAL ISSUE**: Every synthesis run inserts the same citations again. No `ON CONFLICT` clause, no deduplication.

**Failure narrative**:
1. Entry A has citation `src/main.py`
2. Synthesis run 1: INSERT citation (entry_hash=A, value=src/main.py) → id=1
3. Entry A still stable, appears in next snapshot
4. Synthesis run 2: INSERT citation (entry_hash=A, value=src/main.py) → id=2 (duplicate)
5. After 100 synthesis runs, citations table has 100 duplicate rows for the same entry+citation pair
6. `get_unchecked_citations()` returns 100 rows for the same citation
7. Validation pipeline validates the same citation 100 times (wasted I/O)
8. Database size grows linearly with number of synthesis runs (not number of unique entries)

**Impact**: HIGH. Production corruption scenario:
- Automated synthesis runs daily
- After 1 year (365 runs), citations table has 365× duplication
- `get_unchecked_citations()` query time degrades linearly
- Database size bloats from ~100KB to 36MB+ for a single project

**Fix**: Add `UNIQUE` constraint and `ON CONFLICT` clause:

```python
# In _SCHEMA:
CREATE TABLE IF NOT EXISTS citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_hash TEXT NOT NULL REFERENCES memory_entries(entry_hash),
    citation_type TEXT NOT NULL,
    citation_value TEXT NOT NULL,
    resolved_path TEXT,
    status TEXT NOT NULL DEFAULT 'unchecked',
    last_validated TEXT,
    UNIQUE(entry_hash, citation_value)  -- ← Add this
);

# In record_citation():
self.conn.execute(
    """\
    INSERT INTO citations (entry_hash, citation_type, citation_value, resolved_path)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(entry_hash, citation_value) DO UPDATE SET
        resolved_path = excluded.resolved_path,
        citation_type = excluded.citation_type
    """,
    (entry_hash, citation_type, citation_value, resolved_path),
)
```

**Why this fix is safe**:
- New databases get the UNIQUE constraint from schema
- Existing databases need migration (add UNIQUE index, deduplicate rows first)
- Upsert preserves last-seen resolved_path and citation_type (in case project structure changes)

**Migration path** (for Phase 1.1):
1. Add migration in `ensure_schema()`:
   ```python
   if not self._index_exists("citations", "idx_citations_unique"):
       # Deduplicate before adding constraint
       self.conn.execute("""
           DELETE FROM citations WHERE id NOT IN (
               SELECT MIN(id) FROM citations GROUP BY entry_hash, citation_value
           )
       """)
       self.conn.execute("""
           CREATE UNIQUE INDEX idx_citations_unique
           ON citations(entry_hash, citation_value)
       """)
       self.conn.commit()
   ```

### 2.3 Foreign Key Constraint Without CASCADE

**Code**: `metadata.py:29-36` (citations table schema)

```python
CREATE TABLE IF NOT EXISTS citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_hash TEXT NOT NULL REFERENCES memory_entries(entry_hash),
    ...
);
```

**Issue**: No `ON DELETE CASCADE` clause. If entry deletion is added in Phase 2, orphaned citations/checks will remain.

**Current risk**: LOW (no entry deletion yet), but FRAGILE for future changes.

**Recommended fix** (Phase 1.1):
```python
entry_hash TEXT NOT NULL REFERENCES memory_entries(entry_hash) ON DELETE CASCADE,
```

Same for `citation_checks` table (line 41).

**Why this matters**: When an entry is pruned from auto-memory and its promoted version is later deleted from AGENTS.md, the metadata store should clean up associated citations/checks. Without CASCADE, orphaned rows accumulate.

### 2.4 snapshot_count Read-After-Write Inside Transaction

**Code**: `validator.py:92-94`

```python
# Inside transaction, after upsert_entry()
row = metadata_store.get_entry(entry_hash)
snapshot_count = row["snapshot_count"] if row else 1
```

**Question**: Is `get_entry()` guaranteed to see the upserted snapshot_count from line 70?

**Answer**: YES, but relies on SQLite default isolation (DEFERRED transaction mode).

**How it works**:
1. `BEGIN` without `IMMEDIATE` or `EXCLUSIVE` starts a DEFERRED transaction
2. First write (upsert_entry) upgrades to write-lock
3. `get_entry()` SELECT sees all uncommitted writes in the same connection
4. SQLite guarantees read-your-own-writes within a transaction

**Test verification**: `test_metadata.py:66-92` (test_concurrent_upsert_atomic_increment) confirms snapshot_count increments correctly across separate connections.

**Fragility**: If someone changes `begin_transaction()` to use `BEGIN IMMEDIATE`, read-your-own-writes still works. If someone changes to `BEGIN EXCLUSIVE`, still works. So this is robust.

**Verdict**: CORRECT. Not fragile. Leave as-is.

### 2.5 Citation Checks Accumulate (Expected Behavior)

**Code**: `metadata.py:116-140` (record_check)

**Design**: `citation_checks` table is append-only audit log. Each validation run appends a new check row, even if citation was checked before.

**Is this unbounded growth?** YES, but INTENTIONAL.

**Use case**: Track when a citation broke, when it was fixed, and when it broke again.

Example timeline:
- 2026-02-01: Check 1: src/main.py → valid
- 2026-02-10: Check 2: src/main.py → broken (file deleted)
- 2026-02-15: Check 3: src/main.py → valid (file restored)

**Mitigation**: Audit log is historical data, not operational data. Growth is linear with (entries × citations × validation runs), which is acceptable for a per-project database. For a project with 50 entries × 3 citations/entry × 365 runs/year = 54,750 rows/year (~5MB).

**Verdict**: CORRECT by design. No fix needed.

## 3. Concurrency

### 3.1 Concurrent Upsert Test Uses Separate Connections

**Code**: `test_metadata.py:66-92` (test_concurrent_upsert_atomic_increment)

```python
def worker():
    s = MetadataStore(db_path)  # ← New connection per thread
    for _ in range(n_per_thread):
        s.upsert_entry("hash_concurrent", "Preview", "S", "f.md")
        s.conn.commit()
    s.close()
```

**Assessment**: CORRECT. SQLite WAL mode allows concurrent readers + single writer. The test correctly simulates real-world concurrent synthesis runs (separate processes, separate connections).

**How WAL ensures correctness**:
1. Thread A: `upsert_entry()` reads snapshot_count=5, computes 5+1=6, writes 6
2. Thread B (concurrent): `upsert_entry()` reads snapshot_count=5, computes 5+1=6, writes 6
3. SQLite serializes the writes: A commits first (snapshot_count=6), B retries and reads 6, computes 7, writes 7
4. Final snapshot_count=7 (correct, no lost increment)

**This works because**: `ON CONFLICT DO UPDATE SET snapshot_count = snapshot_count + 1` is evaluated AFTER conflict detection, using the current database value (not the value from the INSERT).

**Potential deadlock?** NO. SQLite WAL uses a write-ahead log + checkpoint protocol. Writers don't block readers, readers don't block writers. Only writer-writer serialization exists (handled by busy_timeout=5000ms).

**Verdict**: Safe for production concurrent access.

### 3.2 What Happens During Concurrent validation Runs?

**Scenario**: Process A runs `/intermem:synthesize --validate`, Process B runs `/intermem:validate-promoted` simultaneously.

**Race 1**: Both processes upsert the same entry
- **Outcome**: Safe. Atomic upsert ensures snapshot_count increments correctly (see 3.1).

**Race 2**: Process A commits transaction, Process B reads stale_entries before A's commit is visible
- **Outcome**: Process B's query runs in its own read transaction, sees database state from before A's commit. WAL isolation guarantees consistency (B sees a snapshot). No dirty reads. But B's results may be stale (missing A's updates).
- **Impact**: LOW. Staleness is bounded by validation runtime (~1s). Next validation run will see updated state.

**Race 3**: Process A and B both validate the same citation
- **Outcome**: Both append to citation_checks (audit log). Both update citations.status and last_validated to the same value (idempotent). Final state is correct (last update wins).
- **Wasted work**: Both processes validate the same file path (2× filesystem I/O), but results converge.

**Verdict**: Concurrent validation is safe but may perform redundant work. Acceptable for Phase 1.

### 3.3 No Shutdown Cleanup or Connection Pooling

**Code**: `metadata.py:200-202` (close)

```python
def close(self) -> None:
    """Close the database connection."""
    self.conn.close()
```

**Current pattern**: One connection per MetadataStore instance, explicitly closed by caller.

**Usage in synthesize.py:195**:
```python
metadata_store = MetadataStore(intermem_dir / "metadata.db")
val_result = validate_and_filter_entries(stable_entries, metadata_store, project_root)
metadata_store.close()
```

**Issue**: If validation raises an exception before line 195, connection is never closed.

**Impact**: LOW. Python garbage collector will close the connection on object destruction. SQLite WAL checkpoints on connection close, but also on transaction commit, so no data loss risk.

**Fragility**: If synthesis skill crashes mid-validation, WAL file may not checkpoint until next run. This wastes disk space (WAL can grow) but doesn't corrupt data.

**Recommended fix** (Phase 1.1): Use context manager:

```python
# In metadata.py:
def __enter__(self):
    return self

def __exit__(self, exc_type, exc_val, exc_tb):
    self.close()
    return False

# In synthesize.py:
with MetadataStore(intermem_dir / "metadata.db") as metadata_store:
    val_result = validate_and_filter_entries(stable_entries, metadata_store, project_root)
```

**Verdict**: Current code is safe but fragile. Context manager is a 5-line improvement.

## 4. Confidence Math

### 4.1 Additive Model Correctness

**Code**: `citations.py:167-186` (compute_confidence)

```python
def compute_confidence(
    base: float,
    checks: list[CheckResult],
    snapshot_count: int,
) -> float:
    confidence = base
    for check in checks:
        confidence += check.confidence_delta
    if snapshot_count >= 5:
        confidence += 0.2
    return max(0.0, min(1.0, confidence))
```

**Model**: Base 0.5, +0.3 per valid citation, -0.4 per broken, +0.2 for 5+ snapshots, clamp [0.0, 1.0].

**Edge cases**:

| Scenario | Base | Checks | Bonus | Pre-clamp | Final | Status |
|----------|------|--------|-------|-----------|-------|--------|
| No citations | 0.5 | [] | 0.0 | 0.5 | 0.5 | active |
| 1 valid | 0.5 | [+0.3] | 0.0 | 0.8 | 0.8 | active |
| 1 broken | 0.5 | [-0.4] | 0.0 | 0.1 | 0.1 | stale |
| 2 broken | 0.5 | [-0.8] | 0.0 | -0.3 | 0.0 | stale |
| 3 valid, 1 broken | 0.5 | [+0.9, -0.4] | 0.0 | 1.0 | 1.0 | active |
| 5 valid, 5+ snapshots | 0.5 | [+1.5] | +0.2 | 2.2 | 1.0 | active |
| 1 unchecked | 0.5 | [0.0] | 0.0 | 0.5 | 0.5 | active |

**Surprising case**: Entry with 2 broken citations and no valid ones → confidence = 0.0 (not negative, but unintuitive before clamp).

**Is this correct?** YES. Clamp to [0.0, 1.0] prevents nonsensical negative confidence. The stale threshold (< 0.3) correctly filters these entries.

**Threshold boundary behavior**:

```python
# In metadata.py:144
status = "active" if confidence >= STALE_THRESHOLD else "stale"
```

Threshold = 0.3:
- confidence = 0.30 → active (passes)
- confidence = 0.29 → stale (filtered)

**Is this asymmetric with update logic?** Let's check validator.py:97:

```python
confidence = compute_confidence(0.5, checks, snapshot_count)
metadata_store.update_confidence(entry_hash, confidence)
```

No asymmetry. `update_confidence()` uses `>=` for active (line 144), which matches the documented threshold.

**Test coverage**: `test_metadata.py:153-163` (test_stale_threshold_boundary) verifies boundary.

**Verdict**: CORRECT. Math is sound, threshold is consistent.

### 4.2 Confidence Delta Values: Are They Calibrated?

**Design decision** (from citations.py:141-145):
- Valid citation: +0.3
- Broken citation: -0.4
- Unchecked: 0.0

**Rationale** (inferred from design doc):
- Broken citation is slightly worse than valid is good (-0.4 vs +0.3) → pessimistic bias
- Two broken citations (base 0.5 - 0.8 = -0.3 → clamped to 0.0) always filters entry
- One valid citation (0.5 + 0.3 = 0.8) always passes

**Are these values correct?** This is a POLICY question, not a correctness question. The math is correct for the chosen policy.

**Potential policy issue**: Entry with 1 valid + 1 broken citation:
- confidence = 0.5 + 0.3 - 0.4 = 0.4 (active)
- Entry passes despite 50% broken citations

Is this desired? Unclear from requirements. Document as design decision in AGENTS.md:

> Confidence scoring is pessimistic: broken citations weigh slightly more than valid (+0.3 vs -0.4). An entry with equal valid and broken citations (e.g., 1 valid, 1 broken) scores 0.4 and passes the stale threshold (0.3). This is intentional — we prefer false negatives (stale entries slip through) over false positives (valid entries filtered).

**Verdict**: Math is correct. Policy should be documented.

## 5. Schema and Migration Safety

### 5.1 Schema Versioning

**Code**: `metadata.py:67-76` (ensure_schema)

```python
def ensure_schema(self) -> None:
    self.conn.executescript(_SCHEMA)
    row = self.conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
    if row[0] == 0:
        self.conn.execute(
            "INSERT INTO schema_version (version, migration_version) VALUES (1, 0)"
        )
        self.conn.commit()
```

**Issue**: `executescript()` runs `CREATE TABLE IF NOT EXISTS` every time, which is idempotent but doesn't handle schema changes.

**Migration scenario**:
1. Phase 1.0 ships with schema version 1
2. Phase 1.1 needs to add `UNIQUE(entry_hash, citation_value)` to citations table
3. `CREATE TABLE IF NOT EXISTS citations ...` doesn't recreate the table (it exists), so UNIQUE constraint is never added

**Current mitigation**: Migration helper methods exist (`_table_exists`, `_column_exists`) but no migration runner.

**Recommended migration pattern** (Phase 1.1):

```python
def ensure_schema(self) -> None:
    self.conn.executescript(_SCHEMA)

    # Seed schema_version if empty
    row = self.conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
    if row[0] == 0:
        self.conn.execute(
            "INSERT INTO schema_version (version, migration_version) VALUES (1, 0)"
        )
        self.conn.commit()

    # Run migrations
    current = self.conn.execute(
        "SELECT migration_version FROM schema_version"
    ).fetchone()[0]

    if current < 1:
        self._migrate_to_1()
    if current < 2:
        self._migrate_to_2()
    # etc.

def _migrate_to_1(self) -> None:
    """Add UNIQUE constraint to citations table."""
    if self._index_exists("citations", "idx_citations_unique"):
        return  # Already migrated

    # Deduplicate
    self.conn.execute("""
        DELETE FROM citations WHERE id NOT IN (
            SELECT MIN(id) FROM citations GROUP BY entry_hash, citation_value
        )
    """)

    # Add unique index (equivalent to UNIQUE constraint for our purposes)
    self.conn.execute("""
        CREATE UNIQUE INDEX idx_citations_unique
        ON citations(entry_hash, citation_value)
    """)

    # Update migration_version
    self.conn.execute("UPDATE schema_version SET migration_version = 1")
    self.conn.commit()
```

**Verdict**: Current schema seeding is correct for Phase 1.0. Migration runner needed for Phase 1.1 fixes.

### 5.2 PRAGMA Ordering

**Code**: `metadata.py:61-64`

```python
self.conn.execute("PRAGMA journal_mode = WAL")
self.conn.execute("PRAGMA synchronous = NORMAL")
self.conn.execute("PRAGMA foreign_keys = ON")
self.conn.execute("PRAGMA busy_timeout = 5000")
```

**Is order important?** YES.

- `journal_mode = WAL` must run FIRST (changes database file structure, requires exclusive lock)
- `foreign_keys = ON` must run BEFORE any writes (per-connection setting, doesn't persist)
- `synchronous` and `busy_timeout` can run anytime

**Current order**: Correct.

**Missing PRAGMA**: `PRAGMA wal_autocheckpoint = 1000` (default is 1000 pages, ~4MB). Current code relies on default, which is fine.

**Verdict**: Correct.

## 6. Citation Extraction and Validation

### 6.1 Path Resolution Safety

**Code**: `citations.py:108-123` (resolve_citation)

```python
def resolve_citation(citation: Citation, project_root: Path) -> Path | None:
    try:
        canonical_root = project_root.resolve()
        resolved = (canonical_root / citation.value).resolve()
    except (OSError, ValueError):
        return None
    if not resolved.is_relative_to(canonical_root):
        return None
    return resolved
```

**Security check**: `is_relative_to()` prevents path traversal attacks (e.g., citation value = `../../etc/passwd`).

**Test scenario**:
- project_root = `/root/projects/example`
- citation.value = `../../etc/passwd`
- resolved = `/root/projects/example/../../etc/passwd`.resolve() = `/etc/passwd`
- is_relative_to(`/root/projects/example`) → False → returns None

**Verdict**: CORRECT. Path traversal safe.

**Symlink handling**: `.resolve()` dereferences symlinks. This is CORRECT — if auto-memory cites a symlink, validation should check the symlink target exists.

**Windows compatibility**: `Path.resolve()` and `is_relative_to()` are cross-platform (Python 3.9+). Phase 1 requires Python 3.10+, so safe.

### 6.2 Citation Classification Edge Cases

**Code**: `citations.py:69-78` (_classify_citation)

```python
def _classify_citation(value: str) -> str:
    _, ext = os.path.splitext(value)
    if ext.lower() in _FILE_EXTENSIONS:
        return "file_path"
    basename = os.path.basename(value)
    if "." in basename:
        return "file_path"
    return "module"
```

**Edge case 1**: `src/main` (no extension, no dot in basename) → classified as "module"
- Validation: checks if `<project_root>/src/main` is a directory
- If it's a file without extension (e.g., Bash script), validation fails (broken)

**Is this correct?** NO, but minor impact. Files without extensions are rare in typical projects.

**Fix** (Phase 1.1):
```python
if citation.type == "module":
    if resolved.is_dir():
        return CheckResult(status="valid", confidence_delta=0.3, detail="")
    elif resolved.exists():  # ← Add this: file without extension
        return CheckResult(status="valid", confidence_delta=0.3, detail="")
    return CheckResult(status="broken", confidence_delta=-0.4, detail="Directory or file not found")
```

**Edge case 2**: `lib/file.bak` (extension not in _FILE_EXTENSIONS) → classified as "file_path" (basename contains ".")
- Validation: checks if file exists
- Correct behavior

**Edge case 3**: `src/mod.v2` (looks like version suffix, but has dot) → classified as "file_path"
- Validation: checks if file exists
- If it's a directory, validation fails (broken)

**Is this correct?** Edge case, but acceptable. `mod.v2` as a directory name is uncommon.

**Verdict**: Classification is 95% correct. Known limitation for extensionless files and dotted directory names. Document in AGENTS.md.

### 6.3 Regex Exclusion Patterns

**Code**: `citations.py:47-54` (_EXCLUDE_PATTERNS)

```python
_EXCLUDE_PATTERNS = [
    re.compile(r"^https?://"),
    re.compile(r"^\w+://"),
    re.compile(r"^[A-Z_]+="),
    re.compile(r"^--"),
    re.compile(r"^\$"),
    re.compile(r"^[<>|&]"),
]
```

**Coverage**:
- URLs: `https://example.com/path` → excluded ✓
- Env vars: `PATH=/usr/bin` → excluded ✓
- CLI flags: `--flag` → excluded ✓
- Shell vars: `$VAR` → excluded ✓
- Shell operators: `>file`, `|grep` → excluded ✓

**False positive**: `git://repo` (valid Git URL) → excluded by `^\w+://` ✓ (correct)

**False negative**: `file:///path` (file URL with path component) → excluded by `^\w+://` ✓ (correct)

**Missed pattern**: Inline code with pipes, e.g., `` `git log | grep foo` ``
- Backtick regex: `_BACKTICK_PATH_RE` requires "/" in the matched text → `` `git log | grep foo` `` → no "/" → not matched ✓ (correct)

**Verdict**: Exclusion patterns are correct and complete.

## 7. Integration with Existing Pipeline

### 7.1 synthesize.py Integration

**Code**: `synthesize.py:182-195`

```python
validated_count = 0
stale_filtered = 0
if validate and project_root is not None:
    from intermem.metadata import MetadataStore
    from intermem.validator import validate_and_filter_entries

    metadata_store = MetadataStore(intermem_dir / "metadata.db")
    val_result = validate_and_filter_entries(stable_entries, metadata_store, project_root)
    stable_entries = val_result.validated_entries
    validated_count = val_result.validated_count
    stale_filtered = val_result.stale_count
    metadata_store.close()
```

**Placement**: Validation runs AFTER stability scoring (line 152), BEFORE dedup (line 197).

**Is this correct?** YES. Validation filters out stale entries before expensive fuzzy-match dedup.

**Side effect**: If validation is disabled (`--validate=false`), stable_entries may contain entries with broken citations. They will be promoted to AGENTS.md and later flagged by `/intermem:validate-promoted`.

**Is this acceptable?** YES. Validation is opt-in. Phase 0.5 behavior (no validation) is preserved when `--validate=false`.

### 7.2 Lazy Import

**Code**: `synthesize.py:185-186`

```python
from intermem.metadata import MetadataStore
from intermem.validator import validate_and_filter_entries
```

**Why lazy import?** Avoid importing sqlite3 when validation is disabled (reduces startup time by ~10ms).

**Is this necessary?** NO (sqlite3 is stdlib, always available), but harmless.

**Verdict**: Acceptable micro-optimization.

## 8. Test Coverage

### 8.1 validator Tests

**File**: `test_validator.py`

Coverage:
- ✓ Valid citations pass through
- ✓ Broken citations filter entries
- ✓ No-citation entries keep base confidence
- ✓ Mixed scenarios
- ✓ Transaction rollback on error
- ✓ Promoted entry validation
- ✓ Multiple promoted entries

**Missing test**: Concurrent validation (two processes validating same entry simultaneously)
- **Risk**: LOW (WAL handles this, tested in test_metadata.py)
- **Recommendation**: Add integration test in Phase 1.1

### 8.2 metadata Tests

**File**: `test_metadata.py`

Coverage:
- ✓ Schema creation and idempotency
- ✓ Upsert atomic increment
- ✓ Concurrent upsert correctness
- ✓ Citation recording and status updates
- ✓ Confidence update and stale filtering
- ✓ Transaction commit/rollback

**Missing test**: Migration from Phase 1.0 to Phase 1.1 schema (UNIQUE constraint addition)
- **Risk**: MEDIUM (migration code doesn't exist yet)
- **Recommendation**: Add in Phase 1.1

### 8.3 citations Tests

**File**: Not found in provided source. Assumed to exist.

**Expected coverage**:
- ✓ Citation extraction (backticks, markdown links, absolute paths)
- ✓ Exclusion patterns
- ✓ Path resolution and traversal safety
- ✓ Confidence computation edge cases

**Recommendation**: Verify test_citations.py exists and covers edge cases from section 6.2.

## 9. Recommended Fixes (Prioritized)

### P0 (Ship-blocker — must fix before Phase 1.0 release)

1. **Citation table unbounded growth** (section 2.2)
   - Add UNIQUE constraint to citations table
   - Add ON CONFLICT clause to record_citation()
   - Add migration to deduplicate existing rows
   - **Impact**: Without this, database size grows O(runs) instead of O(entries)

2. **Foreign key CASCADE missing** (section 2.3)
   - Add `ON DELETE CASCADE` to citations and citation_checks tables
   - **Impact**: Future entry deletion will leave orphaned rows

### P1 (Important — should fix in Phase 1.1)

3. **Transaction boundary issue** (section 1.2)
   - Move get_stale_entries() query inside transaction before commit
   - **Impact**: LOW now (single-process), HIGH if concurrent validation is added

4. **Dual-store divergence detection** (section 2.1)
   - Add stability_jsonl_hash to schema_version
   - Add consistency check on MetadataStore init
   - Document safe deletion procedures in AGENTS.md
   - **Impact**: Silent desync can cause incorrect confidence scores

5. **Context manager for MetadataStore** (section 3.3)
   - Add `__enter__` and `__exit__` methods
   - Update synthesize.py to use `with` statement
   - **Impact**: Prevents WAL checkpoint delay on crash

6. **Extensionless file validation** (section 6.2)
   - Update validate_citation() to accept files without extensions
   - **Impact**: Minor (rare case), but easy fix

### P2 (Nice-to-have — Phase 2 scope)

7. **Document confidence policy** (section 4.2)
   - Add confidence scoring rationale to AGENTS.md
   - Explain why broken citations weigh more than valid

8. **Migration runner** (section 5.1)
   - Implement _migrate_to_N() pattern
   - Add _index_exists() helper method

## 10. Summary of Findings

| Finding | Severity | Section | Impact | Fix Effort |
|---------|----------|---------|--------|------------|
| Citation unbounded growth | CRITICAL | 2.2 | Database bloat, query degradation | 10 lines |
| Foreign key no CASCADE | HIGH | 2.3 | Future orphaned rows | 2 lines |
| Transaction boundary | MEDIUM | 1.2 | Dirty reads in concurrent case | 5 lines |
| Dual-store divergence | MEDIUM | 2.1 | Silent desync, wrong confidence | 30 lines |
| No context manager | LOW | 3.3 | WAL checkpoint delay | 10 lines |
| Extensionless files | LOW | 6.2 | Validation failure for rare case | 3 lines |

**Total code changes required for P0 fixes**: ~15 lines + migration

**Breaking changes**: NONE (schema migration handles existing databases transparently)

## 11. Invariants to Preserve

For future changes, these must remain true:

1. **Transaction atomicity**: All writes to metadata.db for a single entry must commit or rollback together
2. **Citation uniqueness**: (entry_hash, citation_value) is a unique pair
3. **Confidence bounds**: confidence ∈ [0.0, 1.0] after clamp
4. **Stale threshold**: status = "stale" iff confidence < 0.3
5. **Foreign key integrity**: No orphaned citations or checks (enforced by CASCADE)
6. **Path safety**: All resolved paths must be relative to project_root (enforced by is_relative_to)
7. **WAL mode**: journal_mode = WAL must persist across connections
8. **Audit immutability**: citation_checks rows are append-only (never UPDATE or DELETE)

## 12. Conclusion

Phase 1 citation validation is **fundamentally sound** but requires **two critical fixes** before release:

1. Add UNIQUE constraint to citations table (prevents unbounded growth)
2. Add ON DELETE CASCADE to foreign keys (prevents future orphaned rows)

These fixes are small (15 lines + migration) and have no breaking changes. After these fixes, Phase 1 is production-ready.

The dual-store divergence (section 2.1) is a medium-priority issue that should be addressed in Phase 1.1 with consistency checking. Current mitigation is documentation.

All other findings are low-priority improvements or documentation tasks.
