# Architecture Review: intermem Plugin — Full Analysis

Reviewed: 2026-02-17
Plugin path: `/root/projects/Interverse/plugins/intermem/`
Codebase mode: project-aware (CLAUDE.md present, AGENTS.md absent)

---

## Scope

Reviewed all source files under `/root/projects/Interverse/plugins/intermem/`:
- `.claude-plugin/plugin.json` — plugin manifest
- `CLAUDE.md` — project quick reference
- `skills/synthesize/SKILL.md` — skill definition and UX contract
- `pyproject.toml` — Python project configuration
- `intermem/__main__.py` — CLI entry point
- `intermem/scanner.py` — markdown parser
- `intermem/stability.py` — snapshot store and scoring
- `intermem/dedup.py` — fuzzy duplicate detection
- `intermem/promoter.py` — target document writer
- `intermem/pruner.py` — auto-memory cleaner
- `intermem/journal.py` — WAL-style promotion journal
- `intermem/synthesize.py` — pipeline orchestrator
- `tests/` — 7 test files, 45 tests

---

## 1. Module Responsibility Assessment

### scanner.py
Single responsibility: parse markdown files from a memory directory into structured `MemoryEntry` objects. Responsibilities are clean. The parser handles section headings, bullet entries, code blocks, and continuation lines within a single `_parse_file` function. The `scan_memory_dir` function delegates to `_parse_file` per file. No output concerns, no I/O beyond reading the memory directory. Rating: clean.

### stability.py
Two responsibilities that are appropriately co-located: (a) persist entry-hash history across runs via `StabilityStore`, and (b) compute stability scores from that history via `score_entries`. These are tightly coupled by design — scoring requires the persisted snapshots, so colocation is correct. One encapsulation issue: `record_snapshot` is a free function in the same module that calls `store._save_snapshot` (a private method), bypassing the class's public interface. Rating: mostly clean, encapsulation issue flagged.

### dedup.py
Single responsibility: compare candidate entries against existing target document content and classify them as novel, exact_duplicate, or fuzzy_duplicate. Uses only `MemoryEntry` from scanner and standard library. No state. Rating: clean.

### journal.py
Single responsibility: append-only WAL journal tracking the pending → committed → pruned lifecycle. Correctly isolated, append-only design, in-memory state backed by JSONL. The `get_incomplete()` query is the sole read interface. Rating: clean, but missing a recovery method that synthesize.py currently owns.

### promoter.py
Single responsibility: write approved entries into a target document under the correct section, appending a new section if needed, with journal tracking. Correctly records pending before write and committed after. Rating: clean.

### pruner.py
Single responsibility: remove committed-and-promoted entries from source memory files. Contains orphaned-header cleanup and backup creation. The only concern is the duplicated `_normalize_content` helper. Rating: mostly clean.

### synthesize.py
Pipeline orchestrator. Should own: sequencing the pipeline stages, passing data between modules, and returning a `SynthesisResult`. Currently also owns: crash recovery logic that reaches into journal internals (lines 41–98) and a broken approval gate stub. The crash recovery function `_recover_incomplete_journal` consults journal status fields, re-checks whether content landed in target docs, and calls `mark_committed` and `mark_pruned` — all journal state transitions. This belongs in `journal.py`. Rating: needs refactor.

### __main__.py
Single responsibility: CLI argument parsing, path discovery, and delegating to `run_synthesis`. Path encoding logic (`_find_memory_dir`) belongs here as a delivery-layer concern. Rating: clean.

---

## 2. Coupling Analysis

### Dependency graph

```
__main__  →  synthesize
synthesize →  scanner, stability, dedup, promoter, pruner, journal
promoter  →  journal, scanner
pruner    →  journal, scanner
stability →  scanner
dedup     →  scanner
```

All dependencies flow toward the core data types (`MemoryEntry`) defined in `scanner.py`. The scanner is the shared data layer. No circular dependencies. The direction is correct — delivery (`__main__`) depends on domain (`synthesize`); domain depends on data (`scanner`).

### Cross-cutting concern: hashing

Three modules independently implement `sha256(text.encode()).hexdigest()[:16]`:
- `stability.py:52` — `_hash_entry(entry)` using `entry.content.strip()` with explicit `"utf-8"`
- `promoter.py:24` — `_hash_content(content)` using `content.strip()` with implicit encoding
- `dedup.py:83,91` — inline, operating on pre-normalized text, no strip

These are not connected by a shared definition. The promoter's hash becomes a journal key used later by the pruner. The pruner does not re-hash; it matches by `_normalize_content`. The dedup hash is used only for the exact-duplicate lookup within a single run. The chains are not cross-module, so the divergence is low risk today but the lack of a shared definition creates drift potential.

### Cross-cutting concern: content normalization

`_normalize_content` (strip each line, rejoin, strip outer whitespace) appears in both `pruner.py:20` and `synthesize.py:30` with identical implementations. Both are module-private. The pruner uses it to match entries against journal content. The synthesize uses it for the same purpose in crash recovery. These should be one function.

### Approval gate coupling

`synthesize.py` lines 184–196 show:
```python
approved = candidates if auto_approve else candidates
if not approved or dry_run:
    return ...
```
The two branches of `auto_approve` are identical. `auto_approve=False` is supposed to trigger an interactive approval loop (presenting a numbered list, reading stdin). That loop does not exist. The tests use `auto_approve=True` exclusively, which masks this gap. The behavior described in `SKILL.md` — numbered candidate list grouped by target, `approve 1,2,4 / reject 3` input — has no implementation.

---

## 3. Plugin Structure Assessment

Interverse plugin conventions (from Interverse `AGENTS.md` and `CLAUDE.md`):
- Plugin manifest at `.claude-plugin/plugin.json` — present and correct
- `skills/<name>/SKILL.md` — present
- `CLAUDE.md` and `AGENTS.md` in project root — only `CLAUDE.md` present
- Python library under `<name>/` package — present as `intermem/`
- Entry point via `pyproject.toml` `[project.scripts]` — present
- `uv run` execution model — `SKILL.md` correctly uses `uv run python -m intermem`

The plugin is correctly structured as a skill-only plugin (no MCP server, no hooks). The `plugin.json` manifest lists only `skills`. The constraint "No hooks (Clavain hook budget)" is documented in `CLAUDE.md`. The missing `AGENTS.md` is a gap against convention.

---

## 4. Data Flow Analysis

### scan → stability → dedup → approve → promote → prune

**scan**: `scan_memory_dir(memory_dir)` returns `ScanResult(entries, total_lines, near_cap)`. Clean.

**stability**: `score_entries(store, entries)` returns `list[StabilityScore]`. Before scoring, `synthesize.py` correctly scores against existing snapshots then records the current snapshot (line 148–149), ensuring the current run's entries are not counted toward their own stability score.

**dedup**: `check_duplicates(stable_entries, target_docs)` returns `list[DedupResult]`. Operates only on stable entries. Extracts all target-doc lines into memory for comparison, O(candidates × target_lines) fuzzy scan. Acceptable at auto-memory scale.

**approve**: Stub (see A1). Effective behavior: all candidates proceed.

**promote**: `promote_entries(approved, target, journal)`. Iterates by section, appends entries under matching section or creates new section. Writes file after each section, which means a multi-section promotion does multiple file reads/writes. Not a correctness issue at this scale.

**prune**: `prune_promoted(approved, memory_dir, journal, dry_run)`. Uses content equality via `_normalize_content` to match entries against journal. Creates `.bak` backup before modifying each source file.

**State files**: `.intermem/stability.jsonl` (hash history), `.intermem/promotion-journal.jsonl` (lifecycle log). Both are append-only JSONL. The journal correctly tracks pending → committed → pruned. On crash, synthesize recovers by checking whether committed content is present in target docs.

### Key invariant

The ordering in `promoter.py` lines 52–80 is correct: `record_pending` before file write, `mark_committed` after file write succeeds. This is the WAL protocol. A crash between write and commit leaves a `pending` entry. On next run, recovery re-checks whether content landed in the target doc (via substring match) and advances to `committed` before pruning.

### Weakness in recovery

The recovery check `_contains_promoted_content(target_path, content)` at `synthesize.py:37` is a raw substring match: `content in text`. This will false-positive if the entry content appears anywhere in the document, not just where it was inserted. For the typical one-liner bullet entries this plugin handles, the risk is low but not zero.

---

## 5. State Management

`.intermem/` directory holds two files. Both use JSONL append-only format — correct for WAL semantics. The stability store loads all snapshots into memory on init; at typical auto-memory sizes (tens to low hundreds of entries, a few dozen snapshots) this is fine. The journal loads the last entry per `entry_hash` (later entries overwrite earlier ones in the in-memory dict), which is the correct behavior for a log that tracks lifecycle transitions.

The `.bak` file created by the pruner is never cleaned up. After each synthesis run that prunes something, `<file>.md.bak` accumulates. There is no rotation, deletion, or reference to this file in the skill documentation. It is a safety net but also an unbounded accumulation.

---

## Summary of Findings

The intermem plugin has correct pipeline semantics, a working WAL crash-recovery design, clean module boundaries at the data layer, and solid test coverage for all stages. The two medium-severity issues are: (1) the interactive approval gate promised by `SKILL.md` is entirely absent — `auto_approve` has no effect and all candidates are promoted without user consent; and (2) crash recovery logic in `synthesize.py` reaches into journal internals and should be a method on `PromotionJournal`. The low-severity issues are: duplicated `_normalize_content` function, three divergent hash implementations, an encapsulation leak in `stability.py`, and a fuzzy-duplicate status label that is computed but never actioned. No circular dependencies, no cross-layer shortcuts, no god modules.

---

## Findings Index (mirrored from quality-gates)

See `/root/projects/Interverse/.clavain/quality-gates/fd-architecture.md` for the canonical findings index, verdict, and improvement list.
