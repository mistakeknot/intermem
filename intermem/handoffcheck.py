"""Stale-claim check for handoff notes.

Re-checks the fast-changing claims in a note (PR state and head, bead status,
task-card status, file hashes, open waits) against their live source and
reports which ones no longer hold. It is read-only: the note is never
modified, and the report is written elsewhere (stdout or ``--out``).

A claim is only flagged STALE when the note asserts something specific (a
state word or a hash right next to the reference) and the live source says
otherwise. A reference with no assertion is reported as NOASSERT; a lookup
that fails is UNRESOLVED. Neither counts as stale.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

# ---- patterns ----------------------------------------------------------

_PR_URL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_PR_SLUG_RE = re.compile(r"\b([\w.-]+/[\w.-]+)#(\d+)\b")
_PR_BARE_RE = re.compile(r"\bPR\s*#?(\d+)\b", re.IGNORECASE)
_CARD_RE = re.compile(r"\b([A-Z]{2,6}-\d+)\b")
_CARD_STOP = {"SHA", "UTF", "TLS", "RFC", "ISO", "HTTP", "GPT", "AES", "CVE", "P"}
_HEAD_RE = re.compile(r"\bhead\s+(?:is\s+|now\s+|at\s+)?`?([0-9a-f]{7,40})\b", re.IGNORECASE)
_SHA256_RE = re.compile(r"\bsha256[\s:=`]*([0-9a-f]{12,64})\b", re.IGNORECASE)
_PATH_RE = re.compile(r"(?<![\w])((?:~|/)[\w./~+-]*[\w])")

_PR_STATES = {"merged": "merged", "open": "open", "closed": "closed"}
_BEAD_STATES = {
    "closed": "closed", "done": "closed", "open": "open",
    "in_progress": "open", "started": "open",
}
_CARD_STATES = {
    "done": "done", "closed": "done", "canceled": "canceled",
    "cancelled": "canceled", "todo": "todo", "open": "todo",
    "in_progress": "in_progress",
}
_CONDITIONAL = re.compile(
    r"\b(?:if|until|when|after|once|before|awaiting|waiting|pending|"
    r"unless|not|never|until)\b[^.;|]{0,30}$", re.IGNORECASE)
_NEGATION = re.compile(r"(?:\bnot|\bnever|n't|\bun)\s*$", re.IGNORECASE)

# ---- data --------------------------------------------------------------

STALE, CURRENT, NOASSERT, UNRESOLVED = "STALE", "CURRENT", "NOASSERT", "UNRESOLVED"


@dataclass
class Claim:
    kind: str            # pr | pr-head | bead | card | hash | wait
    ref: str
    line: int
    asserted: Optional[str]
    text: str


@dataclass
class Result:
    claim: Claim
    verdict: str
    actual: Optional[str] = None
    detail: str = ""


@dataclass
class Resolvers:
    """Live lookups. Each returns None when the source cannot answer."""
    pr: Callable[[str, int], Optional[dict]]
    bead: Callable[[str], Optional[dict]]
    card: Callable[[str], Optional[dict]]
    file_sha256: Callable[[str], Optional[str]]


# ---- extraction --------------------------------------------------------

_COPULA = r"(?:(?:is|are|was|were|now|still|has\s+been|been|got)\s+)*"
_AFTER_STATE = (r"(?=\s*(?:$|[,.;)\]]|\band\b|\bbut\b|\bor\b|\bsince\b|\bon\b|\bat\b|"
                r"\bas\s+of\b|\bwith\b|\bby\b|\bin\b|[(]|[-\u2014]|[0-9a-f]{7,}\b))")


def _state_after(segment: str, table: dict[str, str], before: str) -> Optional[str]:
    """State word directly after a reference ("X is open", "X merged"), with
    only a copula between. Words further along, conditionals ("if X open"),
    negation and imperatives ("open the dashboard") are not assertions."""
    if _CONDITIONAL.search(before):
        return None
    words = "|".join(sorted(map(re.escape, table), key=len, reverse=True))
    m = re.match(r"[\s:,(=\-]*" + _COPULA + r"(" + words + r")\b" + _AFTER_STATE,
                 segment, re.IGNORECASE)
    return table[m.group(1).lower()] if m else None


def extract_claims(text: str, bead_prefixes: tuple[str, ...] = ("mk",),
                   default_repo: Optional[str] = None) -> list[Claim]:
    bead_re = re.compile(
        r"\b((?:%s)-[a-z0-9]{3,6}(?:\.\d+)*)\b" % "|".join(map(re.escape, bead_prefixes)))
    claims: list[Claim] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        refs: list[tuple[int, int, str, str, str]] = []  # start, end, kind, ref, extra
        for m in _PR_URL_RE.finditer(line):
            refs.append((m.start(), m.end(), "pr", f"{m.group(1)}#{m.group(2)}", ""))
        for m in _PR_SLUG_RE.finditer(line):
            if not any(s <= m.start() < e for s, e, *_ in refs):
                refs.append((m.start(), m.end(), "pr", f"{m.group(1)}#{m.group(2)}", ""))
        for m in _PR_BARE_RE.finditer(line):
            if default_repo and not any(s <= m.start() < e for s, e, *_ in refs):
                refs.append((m.start(), m.end(), "pr", f"{default_repo}#{m.group(1)}", ""))
        for m in bead_re.finditer(line):
            refs.append((m.start(), m.end(), "bead", m.group(1), ""))
        for m in _CARD_RE.finditer(line):
            if m.group(1).split("-")[0] in _CARD_STOP:
                continue
            if m.group(1).split("-")[0].lower() in {p.lower() for p in bead_prefixes}:
                continue
            refs.append((m.start(), m.end(), "card", m.group(1), ""))
        refs.sort()
        for i, (s, e, kind, ref, _) in enumerate(refs):
            nxt = refs[i + 1][0] if i + 1 < len(refs) else len(line)
            seg = line[e:nxt]
            before = line[max(0, s - 40):s]
            table = {"pr": _PR_STATES, "bead": _BEAD_STATES, "card": _CARD_STATES}[kind]
            asserted = _state_after(seg, table, before)
            claims.append(Claim(kind, ref, lineno, asserted, line.strip()[:160]))
            if kind == "pr":
                hm = _HEAD_RE.search(seg)
                if hm and not _CONDITIONAL.search(before) and not re.search(
                        r"[.;]|\b(?:not|never|if|until|was|were|unless)\b", seg[:hm.start()],
                        re.IGNORECASE):
                    claims.append(Claim("pr-head", ref, lineno, hm.group(1).lower(),
                                        line.strip()[:160]))
        for hm in _SHA256_RE.finditer(line):
            paths = [m for m in _PATH_RE.finditer(line) if "/" in m.group(1)[1:]]
            ref = ""
            if len(paths) == 1:
                ref = paths[0].group(1)
            elif paths:
                # Several paths: bind to the nearest one, but only if it is
                # clearly nearer than the runner-up; otherwise leave unbound.
                def dist(m):
                    return hm.start() - m.end() if m.end() <= hm.start() else m.start() - hm.end()
                ranked = sorted(paths, key=dist)
                if dist(ranked[1]) - dist(ranked[0]) >= 10:
                    ref = ranked[0].group(1)
            claims.append(Claim("hash", ref, lineno, hm.group(1).lower(), line.strip()[:160]))
    return claims


# ---- checking ----------------------------------------------------------

def _pr_state(info: dict) -> str:
    s = str(info.get("state", "")).lower()
    return "merged" if info.get("mergedAt") or s == "merged" else s


def check_claims(claims: list[Claim], res: Resolvers) -> list[Result]:
    out: list[Result] = []
    cache: dict[tuple, Any] = {}

    def memo(key, fn):
        if key not in cache:
            cache[key] = fn()
        return cache[key]

    for c in claims:
        if c.kind in ("pr", "pr-head"):
            repo, num = c.ref.rsplit("#", 1)
            info = memo(("pr", c.ref), lambda: res.pr(repo, int(num)))
            if not info:
                out.append(Result(c, UNRESOLVED, None, "PR lookup failed"))
                continue
            if c.kind == "pr":
                actual = _pr_state(info)
                if c.asserted is None:
                    out.append(Result(c, NOASSERT, actual))
                elif c.asserted == actual:
                    out.append(Result(c, CURRENT, actual))
                else:
                    out.append(Result(c, STALE, actual, f"note says {c.asserted}"))
            else:
                head = str(info.get("headRefOid", "")).lower()
                if not head:
                    out.append(Result(c, UNRESOLVED, None, "no head"))
                elif head.startswith(c.asserted or "?"):
                    out.append(Result(c, CURRENT, head[:12]))
                else:
                    out.append(Result(c, STALE, head[:12], f"note says head {c.asserted}"))
        elif c.kind == "bead":
            info = memo(("bead", c.ref), lambda: res.bead(c.ref))
            if not info:
                out.append(Result(c, UNRESOLVED, None, "bead lookup failed"))
                continue
            actual = {"closed": "closed"}.get(str(info.get("status")).lower(), "open")
            if c.asserted is None:
                out.append(Result(c, NOASSERT, actual))
            elif c.asserted == actual:
                out.append(Result(c, CURRENT, actual))
            else:
                out.append(Result(c, STALE, actual, f"note says {c.asserted}"))
        elif c.kind == "card":
            info = memo(("card", c.ref), lambda: res.card(c.ref))
            if not info:
                out.append(Result(c, UNRESOLVED, None, "card lookup failed"))
                continue
            actual = str(info.get("status", "")).lower().replace(" ", "_")
            if c.asserted is None:
                out.append(Result(c, NOASSERT, actual))
            elif c.asserted == actual:
                out.append(Result(c, CURRENT, actual))
            else:
                out.append(Result(c, STALE, actual, f"note says {c.asserted}"))
        elif c.kind == "hash":
            if not c.ref:
                out.append(Result(c, UNRESOLVED, None, "no file path on the line"))
                continue
            actual = memo(("hash", c.ref), lambda: res.file_sha256(c.ref))
            if not actual:
                out.append(Result(c, UNRESOLVED, None, "file not readable"))
            elif str(actual).startswith(c.asserted or "?"):
                out.append(Result(c, CURRENT, str(actual)[:12]))
            else:
                out.append(Result(c, STALE, str(actual)[:12], f"note says {(c.asserted or "")[:12]}"))
    return out


def check_waits(text: str, waits_path: Path) -> list[Result]:
    """A wait the note calls open but waits.json has resolved."""
    try:
        data = json.loads(waits_path.read_text())
    except (OSError, ValueError):
        return []
    waits = data.get("waits", []) if isinstance(data, dict) else []
    out: list[Result] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for w in waits:
            wid = str(w.get("id", ""))
            if len(wid) < 4 or not w.get("resolved_at"):
                continue
            m = re.search(
                r"(?<![\w-])%s(?![\w-])[\s:,(=\-]*%s(open|waiting|pending)\b%s"
                % (re.escape(wid), _COPULA, _AFTER_STATE), line, re.IGNORECASE)
            if m and not _CONDITIONAL.search(line[max(0, m.start() - 40):m.start()]):
                c = Claim("wait", wid, lineno, "open", line.strip()[:160])
                out.append(Result(c, STALE, "resolved", f"resolved_at {w['resolved_at']}"))
    return out


# ---- default resolvers -------------------------------------------------

def _run(cmd: list[str], cwd: Optional[str] = None, timeout: int = 30) -> Optional[str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def default_resolvers(bd_dir: Optional[str] = None) -> Resolvers:
    def pr(repo: str, n: int) -> Optional[dict]:
        o = _run(["gh", "pr", "view", str(n), "--repo", repo, "--json", "state,mergedAt,headRefOid"])
        try:
            return json.loads(o) if o else None
        except ValueError:
            return None

    def bead(bid: str) -> Optional[dict]:
        o = _run(["bd", "show", bid, "--json"], cwd=bd_dir)
        try:
            d = json.loads(o) if o else None
        except ValueError:
            return None
        if isinstance(d, list):
            d = d[0] if d else None
        return d

    def card(key: str) -> Optional[dict]:
        o = _run(["bb", "tasks", "show", key, "--json"])
        try:
            d = json.loads(o) if o else None
        except ValueError:
            return None
        t = (d or {}).get("task") or {}
        st = t.get("status") or (t.get("state") or {}).get("name")
        return {"status": st} if st else None

    def file_sha256(path: str) -> Optional[str]:
        try:
            return hashlib.sha256(Path(path).expanduser().read_bytes()).hexdigest()
        except OSError:
            return None

    return Resolvers(pr, bead, card, file_sha256)


# ---- report ------------------------------------------------------------

def render_report(note: Path, results: list[Result]) -> str:
    counts = {v: sum(1 for r in results if r.verdict == v)
              for v in (STALE, CURRENT, NOASSERT, UNRESOLVED)}
    lines = [f"# Stale-claim check: {note.name}", "",
             "Read-only report; the note was not modified.", "",
             "Summary: " + ", ".join(f"{k} {v}" for k, v in counts.items()), ""]
    stale = [r for r in results if r.verdict == STALE]
    if stale:
        lines += ["## Stale", "", "| line | kind | ref | note says | now | claim |", "|---|---|---|---|---|---|"]
        for r in stale:
            lines.append(f"| {r.claim.line} | {r.claim.kind} | {r.claim.ref} | "
                         f"{r.claim.asserted} | {r.actual} | {r.claim.text.replace('|', '/')[:80]} |")
        lines.append("")
    rest = [r for r in results if r.verdict != STALE]
    lines += ["## Not flagged", ""]
    for r in rest:
        lines.append(f"- line {r.claim.line} {r.claim.kind} {r.claim.ref}: {r.verdict}"
                     f"{' (' + r.actual + ')' if r.actual else ''}"
                     f"{' - ' + r.detail if r.detail and r.verdict == UNRESOLVED else ''}")
    return "\n".join(lines) + "\n"


def run_check(note: Path, resolvers: Resolvers, *, bead_prefixes: tuple[str, ...] = ("mk",),
              default_repo: Optional[str] = None, waits: Optional[Path] = None) -> list[Result]:
    text = note.read_text()
    claims = extract_claims(text, bead_prefixes, default_repo)
    results = check_claims(claims, resolvers)
    if waits:
        results += check_waits(text, waits)
    return results
