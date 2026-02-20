# Security and Deployment Safety Review: intermem Phase 1

**Reviewer:** Flux-drive Safety Reviewer
**Date:** 2026-02-17
**Scope:** Citation validation feature (Phase 1) — path resolution, filesystem validation, metadata storage
**Risk Classification:** Medium (path handling from semi-trusted content, filesystem I/O, SQLite storage)

## Executive Summary

intermem Phase 1 adds citation extraction and validation to a memory synthesis plugin. The feature parses auto-memory markdown files (written by AI agents) for file path references, validates them against the project filesystem, and tracks confidence scores in SQLite.

**Security posture: GOOD** — Path traversal defenses are sound, SQL injection risks are minimal, and test coverage validates escape paths. Three medium-priority findings require attention: symlink TOCTOU gap, information leakage via absolute paths, and regex DoS potential.

**Deployment posture: LOW RISK** — Pure additive feature with graceful degradation. Metadata DB is optional (deleting it restores Phase 0 behavior). No migration, no irreversible operations.

---

## Trust Model Analysis

### Threat Boundaries

| Boundary | Untrusted Side | Trusted Side | Enforced By |
|----------|---------------|--------------|-------------|
| Auto-memory content | AI-written markdown (could reference arbitrary paths) | Plugin code | `resolve_citation()` path canonicalization |
| Filesystem access | Resolved citation paths | Project root directory | `Path.resolve()` + `is_relative_to()` |
| Database writes | User-controlled strings in citations | SQLite storage | Parameterized queries |

### Input Trust Levels

1. **Auto-memory markdown content**: Semi-trusted (written by AI during Claude Code sessions, not end-user input, but could contain malicious patterns if AI is adversarially prompted or hallucinates)
2. **Project root parameter** (`project_root: Path`): Trusted (supplied by plugin infrastructure, not extracted from user content)
3. **Citation values** (extracted paths): Untrusted (parsed from semi-trusted markdown using regex)

### Attack Surface

- **Path extraction regex** (`_BACKTICK_PATH_RE`, `_MARKDOWN_LINK_RE`, `_ABSOLUTE_PATH_RE`) — regex DoS potential
- **Path resolution** (`resolve_citation()`) — traversal, symlink escape, TOCTOU
- **SQLite writes** (`metadata.py`) — SQL injection, data leakage
- **Filesystem checks** (`validate_citation()`) — information disclosure via existence oracle

---

## Security Findings

### HIGH PRIORITY: None

### MEDIUM PRIORITY

#### M1: Symlink TOCTOU Gap Between Resolution and Validation

**Location:** `intermem/citations.py:108-123` (resolve), `intermem/citations.py:129-161` (validate)

**Issue:**
`resolve_citation()` correctly rejects symlinks that escape the project root by following them with `.resolve()` and checking `is_relative_to()`. However, there's a time gap between resolution and the subsequent `.exists()` check in `validate_citation()`:

```python
# resolve_citation() @ line 117
resolved = (canonical_root / citation.value).resolve()  # Follows symlink at T0

# validate_citation() @ line 140
if resolved.exists():  # Checks existence at T1
```

**Attack scenario:**
1. Auto-memory references `project/link.txt`
2. At T0 (resolution), `project/link.txt` is a regular file inside project root → resolution succeeds, returns `/abs/path/to/project/link.txt`
3. Attacker replaces `project/link.txt` with symlink to `/etc/passwd` between T0 and T1
4. At T1 (validation), `.exists()` follows new symlink, reads outside project root
5. Result leaks whether `/etc/passwd` exists via confidence score change

**Likelihood:** Low (requires write access to project directory during synthesis run, which implies AI or user already has project write permissions)

**Impact:** Information disclosure (file existence oracle for paths outside project root)

**Mitigation:**
```python
def validate_citation(citation: Citation, project_root: Path) -> CheckResult:
    resolved = resolve_citation(citation, project_root)
    if resolved is None:
        return CheckResult(status="unchecked", confidence_delta=0.0, detail="Cannot resolve path")

    # Re-verify boundary after resolution to close TOCTOU window
    try:
        final_resolved = resolved.resolve(strict=False)
        canonical_root = project_root.resolve()
        if not final_resolved.is_relative_to(canonical_root):
            return CheckResult(status="unchecked", confidence_delta=0.0, detail="Path escaped boundary")
    except (OSError, ValueError):
        return CheckResult(status="unchecked", confidence_delta=0.0, detail="Resolution failed")

    if citation.type == "file_path":
        if resolved.exists():
            return CheckResult(status="valid", confidence_delta=0.3, detail="")
        # ... rest of validation
```

**Residual risk:** TOCTOU is inherent to filesystem operations. Full mitigation requires atomic open-with-boundary-check (not available in stdlib). This mitigation narrows the window but doesn't eliminate it. For a memory synthesis plugin where consequences are limited to confidence score manipulation, residual risk is acceptable.

---

#### M2: Absolute Path Leakage in SQLite Storage

**Location:** `intermem/metadata.py:100-114` (`record_citation()`), `intermem/validator.py:78` (caller)

**Issue:**
`resolved_path` column stores the absolute canonical path returned by `resolve_citation()`:

```python
# validator.py:78-80
resolved = resolve_citation(citation, project_root)
resolved_str = str(resolved) if resolved else None
metadata_store.record_citation(entry_hash, citation.type, citation.value, resolved_str)
```

For a project at `/root/projects/Interverse/plugins/intermem`, a citation `src/main.py` stores `/root/projects/Interverse/plugins/intermem/src/main.py`. This leaks:
1. **Username/home path** (`/root/`, `/home/username/`)
2. **Full directory structure** (could reveal organizational structure, parallel projects)
3. **Filesystem layout** (useful for privilege escalation if attacker gains read access to `.intermem/metadata.db`)

**Threat scenario:**
- `.intermem/` is not gitignored by user (violates constraints in `CLAUDE.md` but is a plausible mistake)
- `metadata.db` is committed to public repo
- OR: attacker gains read-only access to developer machine (malware, insider threat)
- Absolute paths in `resolved_path` column reveal account structure

**Impact:** Information disclosure (low-severity: paths are already visible to anyone with project access, but storing them in a database creates a durable, easily-exfiltrated record)

**Mitigation:**
```python
# validator.py:77-81 (replace)
resolved = resolve_citation(citation, project_root)
if resolved:
    try:
        resolved_str = str(resolved.relative_to(project_root.resolve()))
    except ValueError:
        resolved_str = None  # Should not happen if resolve_citation() works correctly
else:
    resolved_str = None
metadata_store.record_citation(entry_hash, citation.type, citation.value, resolved_str)
```

Store project-relative paths (`src/main.py`) instead of absolute paths. Queries and validation logic remain identical (they re-resolve on read), but stored data no longer leaks filesystem structure.

**Deployment impact:** Existing `metadata.db` files will have absolute paths. Add migration to convert on next run, or document as breaking change (users delete `.intermem/metadata.db` to reset).

---

#### M3: Regex DoS Potential in Citation Extraction

**Location:** `intermem/citations.py:38-44` (regex patterns)

**Issue:**
Three regex patterns parse citations from markdown:

```python
_BACKTICK_PATH_RE = re.compile(r"`([^\s`]+/[^\s`]+)`")
_MARKDOWN_LINK_RE = re.compile(r"\]\(([^)]+/[^)]+)\)")
_ABSOLUTE_PATH_RE = re.compile(r"(?<!\w)(/(?:root|home|usr|opt|etc|var|tmp)/\S+)")
```

**Attack scenario:**
Adversarially crafted auto-memory content with deeply nested structures could cause catastrophic backtracking. Example:

```markdown
- See `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaX` for details
```

For `_BACKTICK_PATH_RE`, the `[^\s`]+` quantifier is greedy and must backtrack if the closing backtick isn't found. Input with 50+ `a` characters and no closing backtick triggers O(2^n) behavior.

**Likelihood:** Low (auto-memory is AI-written, not end-user input; adversarial prompting required)

**Impact:** DoS (synthesis skill hangs, consumes CPU, session timeout)

**Analysis of patterns:**

1. **`_BACKTICK_PATH_RE`**: `[^\s`]+` is greedy but bounded by backticks (which are rare in normal text). Safe for normal content, but unclosed backtick + long run triggers backtracking.
2. **`_MARKDOWN_LINK_RE`**: `[^)]+` is greedy, bounded by `)`. Similar issue with unclosed parens.
3. **`_ABSOLUTE_PATH_RE`**: `\S+` is greedy and unbounded. `/root/` followed by 10,000 non-whitespace characters will scan entire string. Worst offender.

**Mitigation:**

1. **Add possessive quantifiers** (not supported in Python `re`, requires `regex` module):
   ```python
   # Requires: import regex
   _BACKTICK_PATH_RE = regex.compile(r"`([^\s`]++/[^\s`]++)`")
   ```

2. **Add input length limits** (stdlib-compatible):
   ```python
   # citations.py, add at top of extract_citations()
   if len(entry.content) > 50_000:  # 50KB safety limit
       # Log truncation warning, return empty list, or truncate
       return []
   ```

3. **Replace unbounded quantifier in `_ABSOLUTE_PATH_RE`**:
   ```python
   _ABSOLUTE_PATH_RE = re.compile(r"(?<!\w)(/(?:root|home|usr|opt|etc|var|tmp)/\S{1,200})")
   ```
   Limits path length to 200 chars (far above typical, well below DoS threshold).

**Recommended:** Apply mitigation #3 (bounded quantifier) + #2 (global limit). Both are stdlib-compatible and require no new dependencies.

**Testing:** Add to `test_citations.py`:
```python
def test_no_catastrophic_backtracking():
    entry = _entry("- See `" + "a" * 10000)  # Unclosed backtick + long run
    start = time.time()
    citations = extract_citations(entry)
    elapsed = time.time() - start
    assert elapsed < 1.0, f"Regex took {elapsed}s, possible DoS"
    assert len(citations) == 0
```

---

### LOW PRIORITY

#### L1: SQL Injection Surface in `_column_exists()`

**Location:** `intermem/metadata.py:213-215`

```python
def _column_exists(self, table_name: str, column_name: str) -> bool:
    rows = self.conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row["name"] == column_name for row in rows)
```

**Issue:**
`f"PRAGMA table_info({table_name})"` uses f-string interpolation. If `table_name` is user-controlled, this is SQL injection.

**Actual risk:** **NONE** — `_column_exists()` is a private method (leading underscore), only called from migration code with hardcoded table names. No user input path.

**Defense-in-depth recommendation:**
Even though there's no current exploit path, make it injection-proof to prevent future refactoring bugs:

```python
def _column_exists(self, table_name: str, column_name: str) -> bool:
    # SQLite PRAGMA doesn't support parameterized table names, but we can validate
    if not table_name.isidentifier():
        raise ValueError(f"Invalid table name: {table_name}")
    rows = self.conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row["name"] == column_name for row in rows)
```

`isidentifier()` ensures `table_name` matches `[a-zA-Z_][a-zA-Z0-9_]*`, preventing injection.

---

#### L2: Information Leakage via Filesystem Existence Oracle

**Location:** `intermem/citations.py:139-155` (validation logic)

**Issue:**
Validation exposes file/directory existence via confidence delta:
- File exists → +0.3 confidence
- File missing → -0.4 confidence

**Attack scenario:**
1. Attacker prompts AI to write auto-memory entry: `- Config at \`secrets/credentials.json\` must be protected`
2. Synthesis runs, citation validation checks if `project_root/secrets/credentials.json` exists
3. If it exists, entry gets +0.3 confidence; if not, -0.4
4. Attacker observes confidence in `.intermem/metadata.db` (if leaked per M2)

**Actual risk:** **VERY LOW**
- Attacker already needs read access to `.intermem/metadata.db` (local filesystem or git leak)
- If they have that, they likely have direct filesystem access anyway
- Existence oracle is bounded to project root (can't check `/etc/passwd` due to boundary enforcement)

**Mitigation:** None required. This is inherent to validation logic. Document as known behavior.

---

#### L3: Project Root Parameter Not Validated

**Location:** `intermem/synthesize.py:107` (parameter), `intermem/validator.py:56` (usage)

**Issue:**
`run_synthesis()` accepts `project_root: Path | None` from caller, passes it to `validate_and_filter_entries()`, which uses it for boundary checks. If caller provides a malicious `project_root` (e.g., `Path("/")`), boundary checks become ineffective.

**Actual risk:** **NONE**
`run_synthesis()` is called from skill handler (not yet implemented, based on `.claude-plugin/plugin.json`), which will receive `project_root` from Claude Code plugin infrastructure. Plugin infrastructure is trusted.

**Defense-in-depth recommendation:**
Add validation to catch accidental misuse:

```python
def run_synthesis(
    memory_dir: Path,
    target_docs: list[Path],
    intermem_dir: Path,
    auto_approve: bool = False,
    dry_run: bool = False,
    validate: bool = False,
    project_root: Path | None = None,
) -> SynthesisResult:
    # Validate project_root is a subdirectory of /root/projects/ (per Interverse structure)
    if validate and project_root is not None:
        try:
            canonical_root = project_root.resolve()
            # Ensure it's a real directory, not / or /root
            if canonical_root in {Path("/"), Path("/root")}:
                raise ValueError(f"Unsafe project_root: {canonical_root}")
        except (OSError, ValueError) as e:
            raise ValueError(f"Invalid project_root: {e}")
    # ... rest of function
```

Alternatively, require `project_root` to be inside `memory_dir.parent` (since `.claude/memory/` is always inside project root).

---

### INFORMATIONAL

#### I1: Test Coverage for Escape Paths is Excellent

**Location:** `tests/test_citations.py:139-186`

The test suite validates all critical security paths:
- `test_reject_path_traversal()` — `../../etc/passwd` rejected
- `test_reject_absolute_escape()` — `/etc/passwd` rejected
- `test_symlink_escape()` — symlink pointing outside project rejected

These tests directly verify the trust boundary enforcement. No gaps identified.

**Recommendation:** Add regression test for M1 (TOCTOU) after mitigation:
```python
def test_symlink_race_condition(self, tmp_path):
    """Ensure validation re-checks boundary after resolution."""
    (tmp_path / "file.txt").touch()
    citation = Citation(type="file_path", value="file.txt", raw_match="")

    # This test is inherently racy, but documents expected behavior
    resolved = resolve_citation(citation, tmp_path)
    assert resolved is not None

    # Replace with symlink to outside
    (tmp_path / "file.txt").unlink()
    (tmp_path / "file.txt").symlink_to("/etc/passwd")

    # Validation should detect escape even if resolution succeeded
    result = validate_citation(citation, tmp_path)
    assert result.status == "unchecked", "Should reject post-resolution symlink escape"
```

---

#### I2: Parameterized Queries Prevent SQL Injection

**Location:** `intermem/metadata.py:86-153`

All SQL operations use parameterized queries:
```python
self.conn.execute("INSERT INTO ... VALUES (?, ?, ?, ?)", (hash, preview, section, file))
self.conn.execute("UPDATE ... WHERE entry_hash = ?", (hash,))
```

No f-string interpolation in query strings (except `_column_exists()`, covered in L1). SQLite's parameter binding prevents injection.

**Confirmation:** Code review verified. No action needed.

---

## Deployment Safety Analysis

### Migration Path

**Phase 0 → Phase 1:**
- **Schema:** None (metadata.db is created fresh if missing, see `metadata.py:67-76`)
- **Backward compat:** Deleting `.intermem/metadata.db` reverts to Phase 0 behavior (stability tracking only)
- **Rollback:** Delete `.intermem/metadata.db`, restart synthesis

**Risk:** **NONE** — No migration, no data transformation, purely additive.

---

### Rollback Feasibility

| Component | Rollback Action | Data Loss | Complexity |
|-----------|----------------|-----------|------------|
| Code | Git revert | None | Trivial |
| Metadata DB | Delete `.intermem/metadata.db` | Citation history (acceptable) | Trivial |
| JSONL stores | No change (backward compatible) | None | N/A |

**Rollback grade: A+** — Fully reversible by deleting a single file.

---

### Failure Modes

#### Scenario 1: Database Corruption (Power Loss During Write)

**Symptom:** `sqlite3.DatabaseError` on next synthesis run
**Detection:** Exception raised in `validate_and_filter_entries()`, transaction rolled back (line 101)
**Impact:** Validation skipped for current run, synthesis continues with Phase 0 behavior
**Recovery:** Delete `.intermem/metadata.db`, restart synthesis
**Mitigation already in place:**
- WAL mode (line 61): reduces corruption risk
- Transaction wrapping (lines 65, 99-102): atomic commit/rollback

**Operational note:** Add to AGENTS.md troubleshooting:
```markdown
## Troubleshooting

### Synthesis fails with "database is locked"
- **Cause:** Concurrent synthesis runs or stale lock from crash
- **Fix:** `rm .intermem/metadata.db-wal .intermem/metadata.db-shm`, retry
```

---

#### Scenario 2: Malicious Citation Causes Path Resolution Exception

**Symptom:** `OSError` or `ValueError` in `resolve_citation()` (line 118)
**Detection:** Exception caught, returns `None` (line 119)
**Impact:** Citation marked `unchecked`, confidence delta 0.0, entry still processed
**Recovery:** Automatic (graceful degradation)
**User visibility:** Entry appears in synthesis report with "unchecked" citation

**No action needed** — already handled gracefully.

---

#### Scenario 3: Regex DoS Attack (Pre-Mitigation)

**Symptom:** Synthesis skill hangs, no output for >2 minutes
**Detection:** User cancels session, reviews `.claude/memory/` for large entries
**Impact:** Synthesis incomplete, no data loss (JSONL appends are line-atomic)
**Recovery:** Remove problematic auto-memory entry, re-run synthesis
**Mitigation:** Apply M3 (regex length limits)

---

### Pre-Deploy Checklist

- [x] Path traversal boundary checks verified (`resolve_citation()` test coverage complete)
- [x] SQL injection surfaces reviewed (all queries parameterized except safe `_column_exists()`)
- [x] Transaction safety confirmed (validator wraps in BEGIN/COMMIT/ROLLBACK)
- [ ] **REQUIRED:** Apply M3 mitigation (regex DoS limits) before merge
- [ ] **REQUIRED:** Apply M2 mitigation (relative path storage) before next release
- [ ] **RECOMMENDED:** Apply M1 mitigation (TOCTOU re-check) if metadata.db will be exposed to multi-user scenarios
- [ ] **RECOMMENDED:** Add `.intermem/` to project `.gitignore` template (prevent M2 git leak)
- [x] Rollback plan documented (delete metadata.db)
- [ ] Add troubleshooting section to AGENTS.md (corruption recovery, regex DoS)

---

### Post-Deploy Verification

**Day 1 (first synthesis run in production):**
1. Monitor for exceptions in synthesis output (database errors, path resolution failures)
2. Verify `.intermem/metadata.db` created with expected schema version
3. Check citation validation reports for unexpected "unchecked" rates (should be <5%)

**Week 1:**
1. Spot-check metadata.db size (should be <1MB for typical project with <1000 entries)
2. Review stale entry rates (>30% stale = possible broken citation epidemic, investigate)

**Ongoing:**
- No monitoring required (local filesystem, no service dependencies)
- Errors surface in synthesis skill output (user-visible)

---

## Risk Summary

### Exploitable Security Issues: 0
All path traversal defenses validated by tests. SQL injection surfaces minimal and safe.

### Defense-in-Depth Improvements: 3
- **M1 (TOCTOU):** Narrow window, acceptable residual risk for this threat model
- **M2 (Path leakage):** Low severity but easy fix with high payoff (privacy)
- **M3 (Regex DoS):** Medium likelihood in adversarial scenarios, trivial fix

### Deployment Blockers: 0
Rollback is trivial, failure modes degrade gracefully, no irreversible operations.

---

## Recommendations

### Must-Fix Before Merge
1. **Apply M3 mitigation** (regex length limits in `_ABSOLUTE_PATH_RE`, global 50KB cap in `extract_citations()`)
2. Add test case for regex DoS (verify <1s runtime on 10KB adversarial input)

### Should-Fix Before Release
1. **Apply M2 mitigation** (store relative paths in `resolved_path` column)
2. Add `.intermem/` to project `.gitignore` template
3. Document troubleshooting steps in AGENTS.md (corruption recovery)

### Nice-to-Have
1. Apply M1 mitigation (TOCTOU re-check) for defense-in-depth
2. Apply L1 mitigation (`isidentifier()` check in `_column_exists()`)
3. Add L3 validation (reject `project_root` == `/`)

### Architectural Notes for Phase 2

If future phases add:
- **Multi-user synthesis:** M1 (TOCTOU) becomes higher priority
- **Remote metadata sync:** M2 (path leakage) becomes HIGH priority
- **User-supplied memory content:** All regex DoS, path traversal defenses become critical path

Current threat model (single-user, local filesystem, AI-written content) allows graceful degradation and low-risk deployment.

---

## Approval

**Security posture:** APPROVED with required fixes (M3)
**Deployment safety:** APPROVED (rollback trivial, no migration risk)
**Merge gate:** Apply M3 mitigation + test, then merge
**Release gate:** Apply M2 mitigation, add troubleshooting docs

---

## Appendix: Threat Model Validation

### Assumptions Made
1. `.intermem/metadata.db` is local-only (not synced to git or cloud)
2. `project_root` parameter is supplied by trusted plugin infrastructure
3. Auto-memory content is AI-written (semi-trusted, not end-user input)
4. Synthesis runs as same user as Claude Code (no privilege escalation)

### Assumptions to Verify
- [ ] Confirm `.intermem/` gitignore enforcement (check template, plugin docs)
- [ ] Review skill handler implementation when added (verify `project_root` source)

### Out-of-Scope Threats
- **Physical access attacks** (attacker with root can read any file regardless of path checks)
- **Supply chain attacks** (malicious Python stdlib, OS)
- **Side-channel attacks** (timing differences in path resolution)

These are system-level threats outside plugin scope.
