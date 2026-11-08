"""Live-state list for handoff notes.

Lists every reference in a note (PR, bead, task card, thread, wait id, file
hash) with its current state from the live source. It makes no judgement about
what the note says: a reader sees the note's reference and today's state side
by side. It is read-only and meant to be run by hand at session resume.

PRs must be written with a repo (``owner/repo#N`` or a pull-request URL); a
bare "PR 12" is listed as unqualified and never looked up. A lookup that fails
is shown as ``unresolved``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

_PR_URL_RE = re.compile(r"(?<![\w./:-])(?:https?://(?:www\.)?)?github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_PR_SLUG_RE = re.compile(r"(?<![\w/.-])([\w.-]+/[\w.-]+)#(\d+)\b")
_PR_BARE_RE = re.compile(r"\bPR\s*#?(\d+)\b", re.IGNORECASE)
_CARD_RE = re.compile(r"\b([A-Z]{2,6}-\d+)\b")
_CARD_STOP = {"SHA", "UTF", "TLS", "RFC", "ISO", "HTTP", "GPT", "AES", "CVE", "P"}
_THREAD_RE = re.compile(r"\b(thr_[a-z0-9]{10})\b")
# absolute, ~/ or ./ ../ paths (relative ones resolve against the cwd)
_PATH_RE = re.compile(r"(?<![\w.~/])((?:~|\.{1,2})?/[\w./~+-]*[\w])")
_WAIT_RE = re.compile(r"(?<![\w-])([a-z]+(?:-[\w.]+)+-[0-9a-f]{8})(?![\w-])")
_MAX_HASH_BYTES = 64 * 1024 * 1024
_SHA_WORD_RE = re.compile(r"sha-?256", re.IGNORECASE)

UNRESOLVED = "unresolved"
UNQUALIFIED = "unqualified (write owner/repo#N)"


@dataclass
class Ref:
    kind: str       # pr | bead | card | thread | wait | file
    ref: str
    line: int
    state: str
    detail: str = ""


@dataclass
class Resolvers:
    pr: Callable[[str, int], Optional[dict]]
    bead: Callable[[str], Optional[dict]]
    card: Callable[[str], Optional[dict]]
    thread: Callable[[str], Optional[dict]]
    file_sha256: Callable[[str], Optional[str]]


def extract_refs(text: str, bead_prefixes: tuple[str, ...] = ("mk",)) -> list[tuple[str, str, int]]:
    """Return (kind, ref, line) for each distinct reference per line."""
    bead_re = re.compile(r"\b((?:%s)-[a-z0-9]{3,6}(?:\.\d+)*)\b" % "|".join(map(re.escape, bead_prefixes)))
    out: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str, int]] = set()

    def add(kind: str, ref: str, n: int) -> None:
        key = (kind, ref, n)
        if key not in seen:
            seen.add(key)
            out.append(key)

    for n, line in enumerate(text.splitlines(), 1):
        for m in _PR_URL_RE.finditer(line):
            add("pr", f"{m.group(1)}#{m.group(2)}", n)
        stripped = _PR_URL_RE.sub(" ", line)
        for m in _PR_SLUG_RE.finditer(stripped):
            add("pr", f"{m.group(1)}#{m.group(2)}", n)
        no_slug = _PR_SLUG_RE.sub(" ", stripped)
        for m in _PR_BARE_RE.finditer(no_slug):
            add("pr", f"PR {m.group(1)}", n)
        for m in bead_re.finditer(line):
            add("bead", m.group(1), n)
        for m in _CARD_RE.finditer(line):
            c = m.group(1)
            if c.split("-")[0] in _CARD_STOP or c.split("-")[0].lower() in bead_prefixes:
                continue
            add("card", c, n)
        for m in _THREAD_RE.finditer(line):
            add("thread", m.group(1), n)
        if _SHA_WORD_RE.search(line):
            for m in _PATH_RE.finditer(re.sub(r"\w+://\S+", " ", line)):
                add("file", m.group(1), n)
    return out


def _pr_state(info: dict) -> str:
    if info.get("mergedAt") or str(info.get("state", "")).lower() == "merged":
        return "merged"
    return str(info.get("state", "?")).lower()


def resolve_refs(refs: list[tuple[str, str, int]], res: Resolvers) -> list[Ref]:
    cache: dict[tuple, object] = {}

    def memo(key: tuple, fn: Callable[[], object]) -> object:
        if key not in cache:
            cache[key] = fn()
        return cache[key]

    out: list[Ref] = []
    for kind, ref, line in refs:
        if kind == "pr":
            if ref.startswith("PR "):
                out.append(Ref(kind, ref, line, UNQUALIFIED))
                continue
            repo, num = ref.rsplit("#", 1)
            info = memo(("pr", ref), lambda: res.pr(repo, int(num)))
            if not isinstance(info, dict):
                out.append(Ref(kind, ref, line, UNRESOLVED))
            else:
                head = str(info.get("headRefOid") or "")[:12]
                out.append(Ref(kind, ref, line, _pr_state(info), f"head {head}" if head else ""))
        elif kind == "bead":
            info = memo(("bead", ref), lambda: res.bead(ref))
            st = info.get("status") if isinstance(info, dict) else None
            out.append(Ref(kind, ref, line, str(st) if st else UNRESOLVED))
        elif kind == "card":
            info = memo(("card", ref), lambda: res.card(ref))
            st = info.get("status") if isinstance(info, dict) else None
            out.append(Ref(kind, ref, line, str(st) if st else UNRESOLVED))
        elif kind == "thread":
            info = memo(("thread", ref), lambda: res.thread(ref))
            if not isinstance(info, dict):
                out.append(Ref(kind, ref, line, UNRESOLVED))
            else:
                st = str(info.get("status") or "?")
                if info.get("archivedAt"):
                    st += ", archived"
                out.append(Ref(kind, ref, line, st))
        elif kind == "file":
            h = memo(("file", ref), lambda: res.file_sha256(ref))
            out.append(Ref(kind, ref, line, f"sha256 {str(h)[:12]}" if h else "missing or unreadable"))
        else:
            out.append(Ref(kind, ref, line, UNRESOLVED))
    return out


def wait_refs(text: str, waits_path: Path) -> list[Ref]:
    """Wait ids in the note, looked up in waits.json.

    An id that looks like a wait id but is not in the registry, or any id when
    the registry cannot be read, is shown as unresolved.
    """
    lines = text.splitlines()
    try:
        data = json.loads(waits_path.read_text())
    except (OSError, ValueError):
        data = None
    items = data.get("waits") if isinstance(data, dict) else None
    known: dict[str, dict] = {}
    if isinstance(items, list):
        for w in items:
            if isinstance(w, dict) and isinstance(w.get("id"), str) and w["id"]:
                known[w["id"]] = w
    out: list[Ref] = []
    done: set[str] = set()
    for n, line in enumerate(lines, 1):
        cands = [m.group(1) for m in _WAIT_RE.finditer(line)]
        cands += [wid for wid in known if wid not in cands and re.search(
            r"(?<![\w-])" + re.escape(wid) + r"(?![\w-])", line)]
        for wid in cands:
            if wid in done:
                continue
            done.add(wid)
            w = known.get(wid)
            if w is None:
                out.append(Ref("wait", wid, n, UNRESOLVED))
                continue
            state = f"resolved {w['resolved_at']}" if w.get("resolved_at") else "open"
            out.append(Ref("wait", wid, n, state, f"{w.get('kind', '')} {w.get('ref', '')}".strip()))
    return out


def _run(cmd: list[str], cwd: Optional[str] = None, timeout: int = 30) -> Optional[str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def _json(o: Optional[str]) -> object:
    try:
        return json.loads(o) if o else None
    except ValueError:
        return None


def default_resolvers(bd_dir: Optional[str] = None) -> Resolvers:
    def pr(repo: str, n: int) -> Optional[dict]:
        d = _json(_run(["gh", "pr", "view", str(n), "--repo", repo,
                        "--json", "state,mergedAt,headRefOid"]))
        return d if isinstance(d, dict) else None

    def bead(bid: str) -> Optional[dict]:
        d = _json(_run(["bd", "show", bid, "--json"], cwd=bd_dir))
        if isinstance(d, list):
            d = d[0] if d else None
        return d if isinstance(d, dict) else None

    def card(key: str) -> Optional[dict]:
        d = _json(_run(["bb", "tasks", "show", key, "--json"]))
        t = (d or {}).get("task") if isinstance(d, dict) else None
        t = t if isinstance(t, dict) else {}
        st = t.get("status") or (t.get("state") or {}).get("name")
        return {"status": st} if st else None

    def thread(tid: str) -> Optional[dict]:
        d = _json(_run(["bb", "thread", "show", tid, "--json"]))
        t = d.get("thread") if isinstance(d, dict) else None
        return t if isinstance(t, dict) else None

    def file_sha256(path: str) -> Optional[str]:
        # Regular files only, bounded size, hashed in chunks: the path comes
        # from the note, so a device or FIFO must not hang or exhaust memory.
        try:
            p = Path(path).expanduser()
            if not stat.S_ISREG(p.stat().st_mode) or p.stat().st_size > _MAX_HASH_BYTES:
                return None
            h = hashlib.sha256()
            fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
            seen = 0
            with os.fdopen(fd, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    seen += len(chunk)
                    if seen > _MAX_HASH_BYTES:
                        return None
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return None

    return Resolvers(pr, bead, card, thread, file_sha256)


def render(note: Path, refs: list[Ref]) -> str:
    lines = [f"# Live state of references in {note.name}", "",
             "Read-only list; the note was not modified and nothing here says whether the note is right.", ""]
    if not refs:
        return "\n".join(lines + ["No references found."]) + "\n"
    lines += ["| line | kind | reference | state now | detail |", "|---|---|---|---|---|"]
    for r in sorted(refs, key=lambda r: (r.line, r.kind, r.ref)):
        lines.append(f"| {r.line} | {r.kind} | {r.ref} | {r.state} | {r.detail} |")
    return "\n".join(lines) + "\n"


def run_live_state(note: Path, res: Resolvers, *, bead_prefixes: tuple[str, ...] = ("mk",),
                   waits: Optional[Path] = None) -> list[Ref]:
    text = note.read_text()
    refs = resolve_refs(extract_refs(text, bead_prefixes), res)
    if waits:
        refs += wait_refs(text, waits)
    return refs


def write_report(out: Path, note: Path, body: str) -> None:
    """Write body to out without ever touching the note.

    Refuses the note, a hard link or symlink to it; writes an exclusive temp
    file in the output directory and renames it into place.
    """
    if out.resolve() == note.resolve() or (out.exists() and os.path.samefile(out, note)):
        raise ValueError("--out must not be the note itself")
    fd, tmp = tempfile.mkstemp(dir=out.resolve().parent, prefix=".ls-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        os.chmod(tmp, 0o644)
        os.replace(tmp, out)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
