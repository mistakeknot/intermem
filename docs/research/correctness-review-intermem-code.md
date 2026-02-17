# Correctness Review: intermem Plugin Code

Reviewed by: Julik (Flux-drive Correctness Reviewer)
Date: 2026-02-17

Full findings written to: `/root/projects/Interverse/.clavain/quality-gates/fd-correctness.md`

---

## Summary

The journal-backed WAL design is architecturally sound, but two high-severity gaps undermine it. The promoter writes the target file once per section in a loop, so a crash between sections can leave the file in a partially updated state while some journal entries remain "pending" — recovery logic then risks re-inserting already-written content. Additionally, the `auto_approve` flag in `synthesize.py` is dead code (both branches assign `candidates`), meaning the system always auto-promotes all candidates including fuzzy duplicates, with no human gate. A 16-character hash truncation used as the journal key creates a silent-suppression risk when two different entries produce the same prefix.

### High-Severity Issues

- **C1 (HIGH, `promoter.py:49-80`):** Per-section `write_text` inside a loop creates a crash window where the target file is partially updated but the journal is not fully committed. Recovery may re-insert already-written sections, producing duplicate content.
- **C2 (HIGH, `journal.py:68-70`, `promoter.py:24-25`):** Identical entries from different source files share the same 16-char hash key; the second entry's `record_pending` is silently suppressed if the first is still pending/committed, permanently stranding it in auto-memory.

### Medium-Severity Issues

- **C3 (MEDIUM, `synthesize.py:167-170`):** Fuzzy-duplicate entries (similar but not identical to existing target-doc content) are promoted unconditionally, polluting target docs with near-duplicate facts.
- **C5 (MEDIUM, `journal.py`):** No file lock on journal writes; two concurrent `run_synthesis` invocations can both see the journal as clean and both promote the same entry, inserting it twice.
- **C6 (MEDIUM, `synthesize.py:185`):** `approved = candidates if auto_approve else candidates` — both branches are identical; `--auto-approve` has zero effect and no interactive approval gate exists.

### Low-Severity Issues

- **C7 (LOW, `scanner.py:64`):** Unclosed code fence at end of file silently swallows all subsequent entries with no error or warning.
- **C8 (LOW, `pruner.py:142-143`):** Single `.bak` per file; rapid successive prune runs overwrite the backup, eroding the safety net.
- **C9 (LOW, `dedup.py:89-91`):** Exact-duplicate hash covers only the first line of a multi-line entry; re-promoted multi-line entries slip through dedup as novel.
- **C10 (LOW, `stability.py:39-43`, `journal.py:46-62`):** Both JSONL files grow without bound; no compaction or rotation.

### Verdict: needs-changes

The two HIGH issues (C1, C2) and the dead-code bug (C6) should be fixed before the plugin handles real production memory files. The rest are well-bounded risks suitable for the next iteration.
