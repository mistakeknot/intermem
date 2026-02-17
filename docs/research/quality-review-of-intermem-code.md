# Quality Review: intermem Plugin Python Code

Reviewed: 2026-02-17
Scope: All 8 source modules and 7 test files. 45 tests passing.

---

## Overall Assessment

The codebase is well-structured for its problem domain. Modules have clear single responsibilities, dataclasses are used appropriately throughout, `from __future__ import annotations` is applied consistently, and the WAL-style journal design reflects thoughtful durability engineering. The test suite covers the happy paths and several boundary conditions. Two correctness bugs stand out as high priority fixes; the rest are moderate reliability and maintainability issues.

---

## Findings Index

| SEVERITY | ID | Section | Title |
|----------|----|---------|-------|
| HIGH | F-01 | synthesize.py | Dead assignment makes `auto_approve` a no-op |
| HIGH | F-02 | journal.py | `mark_committed`/`mark_pruned` raise `KeyError` on unknown hash |
| HIGH | F-03 | stability.py | Module-level function calls private class method from outside the class |
| MEDIUM | F-04 | pruner.py | Dry-run `new_total` calculation inverts sign of delta |
| MEDIUM | F-05 | promoter.py | File written per-section in loop — partial write not rolled back |
| MEDIUM | F-06 | journal.py | `_load` has no JSONL parse-error handling |
| MEDIUM | F-07 | stability.py | `_load` has no JSONL parse-error handling |
| LOW | F-08 | pruner.py | `.bak` file left permanently on disk |
| LOW | F-09 | scanner.py | `_make_entry` mutates caller's `lines` list in place |
| LOW | F-10 | synthesize.py / pruner.py | `_normalize_content` duplicated verbatim |
| INFO | F-11 | dedup.py / stability.py / journal.py | Status string fields should use `Literal` types |
| INFO | F-12 | stability.py | `_snapshots` typed as unbound `list[dict]` |

Verdict: needs-changes

---

## Detailed Findings

### F-01. HIGH — Dead assignment makes `auto_approve` a no-op

**File:** `intermem/synthesize.py`, line 185

```python
approved = candidates if auto_approve else candidates
```

Both branches of the conditional assign `candidates`. The `auto_approve` parameter has zero effect on runtime behavior. The pipeline always treats every novel/fuzzy candidate as approved, defeating the intended interactive-approval gate. The `--auto-approve` CLI flag is tested in integration tests that pass because candidates always end up approved regardless.

**Fix:** Implement the intended branching. Without interactive approval logic built out yet, the false branch should return early or set `approved = []`:

```python
if not auto_approve:
    return SynthesisResult(
        ...
        promoted_count=0,
        ...
    )
approved = candidates
```

---

### F-02. HIGH — `mark_committed`/`mark_pruned` raise `KeyError` on unknown hash

**File:** `intermem/journal.py`, lines 85 and 98

```python
def mark_committed(self, entry_hash: str) -> None:
    entry = self._entries[entry_hash]   # KeyError if not found
```

```python
def mark_pruned(self, entry_hash: str) -> None:
    entry = self._entries[entry_hash]   # KeyError if not found
```

The pruner wraps its call to `mark_pruned` in a `try/except KeyError: continue` (pruner.py:153-155), but `mark_committed` is called from the promoter with no protection. If a hash is missing from `_entries` — possible after a crash that left the journal in an inconsistent state — the promoter raises an uncaught exception mid-promotion.

**Fix:** Raise a more descriptive `ValueError` with context, or guard with `.get()` and raise only if the state transition is genuinely unexpected:

```python
def mark_committed(self, entry_hash: str) -> None:
    entry = self._entries.get(entry_hash)
    if entry is None:
        raise ValueError(f"Cannot mark committed: unknown journal entry {entry_hash!r}")
    ...
```

---

### F-03. HIGH — Module-level function accesses private method of `StabilityStore`

**File:** `intermem/stability.py`, line 106

```python
def record_snapshot(store: StabilityStore, entries: list[MemoryEntry]) -> None:
    ...
    store._save_snapshot(snapshot)   # calling a private method from outside the class
```

This crosses the encapsulation boundary. The `_save_snapshot` method is part of the store's internal write path, but it is now effectively a public API. Any change to the internal write mechanism requires auditing both the class and the module-level function.

**Fix:** Make `record_snapshot` a public method on `StabilityStore`:

```python
class StabilityStore:
    def record(self, entries: list[MemoryEntry]) -> None:
        snapshot_entries = [...]
        snapshot = {"timestamp": ..., "entries": snapshot_entries}
        self._save_snapshot(snapshot)
```

---

### F-04. MEDIUM — Dry-run `new_total` calculation inverts sign of delta

**File:** `intermem/pruner.py`, lines 134, 158-160

```python
file_deltas[source_file] = (len(original_lines), len(cleaned_lines))  # (old, new)
...
delta = sum(new - old for old, new in file_deltas.values())
new_total = max(0, current_total + delta)
```

`file_deltas` stores `(original_count, cleaned_count)`. The variable names in the comprehension are `old, new` matching the tuple order. For a prune operation, `cleaned_count < original_count`, so `new - old` is negative. Adding a negative delta to `current_total` happens to produce the right sign — but the semantics expressed are inverted: a positive `delta` means lines grew, but conceptually "delta of a prune" should mean lines removed (positive). The result is fragile and misleading.

**Fix:** Store as `(removed)` or invert the computation to be explicit:

```python
lines_removed_per_file = sum(orig - cleaned for orig, cleaned in file_deltas.values())
new_total = max(0, current_total - lines_removed_per_file)
```

---

### F-05. MEDIUM — Promoter writes to disk per-section inside loop

**File:** `intermem/promoter.py`, lines 49-80

```python
for section_name, section_entries in by_section.items():
    ...
    target_path.write_text(content)    # line 75: write inside loop
    for entry in section_entries:
        journal.mark_committed(entry_hash)   # line 80
```

When there are two or more sections, the first section is written and journaled as `committed`. If an exception occurs during the second section (regex failure, encoding error, permissions), the file is in a partial state: section 1 was written to disk but the journal may not reflect it accurately, and section 2 was not written. The crash-recovery path in `synthesize.py` attempts to handle this but relies on content presence checks that may not match.

**Fix:** Accumulate all insertions into `content` across the full loop, then do a single write and batch-commit all journal entries after the write succeeds:

```python
all_pending_hashes: list[str] = []
for section_name, section_entries in by_section.items():
    for entry in section_entries:
        entry_hash = _hash_content(entry.content)
        journal.record_pending(...)
        all_pending_hashes.append(entry_hash)
    # mutate content only
    ...

target_path.write_text(content)  # single write

for entry_hash in all_pending_hashes:
    journal.mark_committed(entry_hash)
```

---

### F-06. MEDIUM — `journal.py` `_load` does not handle malformed JSONL

**File:** `intermem/journal.py`, lines 39-44

```python
for line in self.path.read_text().splitlines():
    if not line.strip():
        continue
    data = json.loads(line)   # raises json.JSONDecodeError on truncated lines
    entry = JournalEntry(**data)
    self._entries[entry.entry_hash] = entry
```

A crash mid-write can leave a partial JSON line at the end of the file. The next startup raises `json.JSONDecodeError` and crashes `PromotionJournal.__init__`. Since the journal is a crash-recovery mechanism, it must be resilient to crash-induced corruption in the file it manages.

**Fix:**

```python
try:
    data = json.loads(line)
except json.JSONDecodeError:
    # Truncated line from a prior crash — skip and continue
    continue
```

---

### F-07. MEDIUM — `stability.py` `_load` does not handle malformed JSONL

**File:** `intermem/stability.py`, lines 34-37

Same pattern as F-06. `json.loads(line)` without error handling in `StabilityStore._load`. A partial write to `stability.jsonl` on crash will prevent the store from loading.

**Fix:** Same pattern as F-06 — wrap in `try/except json.JSONDecodeError: continue`.

---

### F-08. LOW — `.bak` file left permanently on disk after every prune

**File:** `intermem/pruner.py`, lines 142-143

```python
backup_path = file_path.with_suffix(file_path.suffix + ".bak")
backup_path.write_text(original_text, encoding="utf-8")
```

After a successful write, the `.bak` file is never removed. Repeated prune runs leave accumulating `.bak` files in the memory directory. The `.bak` extension means `scan_memory_dir` (which only globs `*.md`) won't pick them up, but they are left as litter. If the retention is intentional, document it and add to `.gitignore` guidance in CLAUDE.md. If not, remove after successful write:

```python
backup_path.unlink(missing_ok=True)
```

---

### F-09. LOW — `_make_entry` mutates caller's list via `list.pop()`

**File:** `intermem/scanner.py`, lines 124-126

```python
def _make_entry(lines: list[str], ...) -> MemoryEntry:
    while lines and not lines[-1].strip():
        lines.pop()   # mutates the caller's accumulator in place
```

The function modifies its `lines` argument. This works today only because the caller (`_parse_file`) immediately re-assigns `current_entry_lines = []` after each call. Any future caller that inspects the list after the call will see a silently truncated version.

**Fix:** Copy at the start of the function:

```python
def _make_entry(lines: list[str], ...) -> MemoryEntry:
    lines = list(lines)  # work on a copy
    while lines and not lines[-1].strip():
        lines.pop()
```

---

### F-10. LOW — `_normalize_content` duplicated between `synthesize.py` and `pruner.py`

**Files:** `intermem/synthesize.py:30-31`, `intermem/pruner.py:20-22`

```python
# synthesize.py
def _normalize_content(content: str) -> str:
    return "\n".join(line.strip() for line in content.splitlines()).strip()

# pruner.py — identical
def _normalize_content(content: str) -> str:
    return "\n".join(line.strip() for line in content.splitlines()).strip()
```

The normalization is the semantic glue between the promoter's stored content and the pruner's content-matching. If one copy is updated (e.g., to handle Windows line endings or leading bullet indentation) and the other is not, the journal matching silently breaks and promoted entries are never pruned.

**Fix:** Extract to `intermem/_util.py` and import in both modules.

---

### F-11. INFO — Status/label string fields should use `Literal` types

**Files:** `dedup.py:23`, `stability.py:19`, `journal.py:18`

- `DedupResult.status: str` — values are `"novel"`, `"exact_duplicate"`, `"fuzzy_duplicate"`
- `StabilityScore.score: str` — values are `"stable"`, `"recent"`, `"volatile"`
- `JournalEntry.status: str` — values are `"pending"`, `"committed"`, `"pruned"`

Using `str` loses type narrowing. Callers comparing `.status == "exact_duplicate"` get no IDE warning on typos. Adding `Literal` types (Python 3.8+, already used in this codebase via `from __future__ import annotations`) costs nothing and improves correctness.

```python
from typing import Literal

@dataclass
class DedupResult:
    status: Literal["novel", "exact_duplicate", "fuzzy_duplicate"]
    ...
```

---

### F-12. INFO — `StabilityStore._snapshots` typed as unbound `list[dict]`

**File:** `intermem/stability.py:28`

```python
self._snapshots: list[dict] = []
```

The snapshot schema is well-defined: `{"timestamp": str, "entries": list[{"hash": str, "section": str, "preview": str}]}`. Typing as `list[dict[str, object]]` is a minimal improvement; a `TypedDict` for the snapshot schema would make `_iter_snapshot_hash_sections` easier to read and statically verify.

---

## Test Coverage Observations

The test suite is well-organised with descriptive names and clear arrange/act/assert structure. A few gaps worth noting:

1. **No test for `auto_approve=False`** — once F-01 is fixed, a test confirming the no-approve path exits without promotion is needed.
2. **No test for malformed JSONL** — after fixing F-06/F-07, tests that create corrupted state files and verify graceful recovery would guard against regression.
3. **`test_pruner.py` uses bare short hash strings** (`"fakehash"`, `"h"`) — these don't collide in tests but could mask bugs where real 16-char hex hashes are expected. Using `hashlib.sha256(b"x").hexdigest()[:16]` patterns in tests would be more representative.
4. **`test_synthesize.py`** is effectively an integration test suite. It exercises the full pipeline well. The `test_pruning_after_promotion` test indirectly validates journal-driven pruning end-to-end, which is the highest-risk code path.

---

## Improvements

**I-01.** Make `record_snapshot` a public `StabilityStore.record(entries)` method to eliminate private-method access from outside the class.

**I-02.** Introduce `Literal` type aliases for all status/label fields in `JournalEntry`, `StabilityScore`, and `DedupResult` to gain static narrowing and typo detection.

**I-03.** Extract `_normalize_content` to a shared `intermem/_util.py` module to eliminate the duplication risk between `synthesize.py` and `pruner.py`.

**I-04.** Add `try/except json.JSONDecodeError` wrappers in `PromotionJournal._load` and `StabilityStore._load` so a crash-truncated JSONL trailing line is skipped with a warning rather than crashing startup.

**I-05.** Restructure `promote_entries` to accumulate all content mutations in memory, write once, then batch-commit journal entries, to satisfy the WAL atomicity promise it advertises.

**I-06.** Add `test_auto_approve_false_does_not_promote` to `test_synthesize.py` once F-01 is fixed.

**I-07.** Document `.bak` retention intent or delete backups after successful prune. Add `.intermem/*.bak` to `.gitignore` guidance in CLAUDE.md.
