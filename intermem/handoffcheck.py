"""Stale-claim check for handoff notes.

Re-checks the fast-changing claims in a note (PR state and head, bead status,
task-card status, file hashes, open waits) against their live source and
reports which ones no longer hold. It is read-only: the note is never
modified, and the report is written elsewhere (stdout or ``--out``).

A claim is only flagged STALE when the note states it in an explicit form
("REF: merged", "REF (closed)", "REF: head SHA", a line holding just
"PATH sha256 HEX") and the live source says otherwise; free text is ignored. A reference with no assertion is reported as NOASSERT; a lookup
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

# Only explicit forms count as an assertion (option B: no free-text matching):
#   REF: STATE        REF (STATE)        REF: head SHA        REF (STATE, head SHA)
# and nothing else on the clause. "REF open or closed?", "REF open, if CI
# passes" and "REF open the dashboard" are therefore never assertions.
_TAIL = r"\s*\)?\s*(?:$|[.;](?:\s|$))"


def _explicit(seg: str, table: dict[str, str]) -> Optional[tuple[Optional[str], Optional[str]]]:
    """Parse the text right after a reference. Returns (state, head) when it
    is exactly one of the explicit forms, else None."""
    words = "|".join(sorted(map(re.escape, table), key=len, reverse=True))
    m = re.match(
        r"\s*[:(]\s*(?:(?P<st>%s)\b)?(?:\s*,\s*)?(?:head\s+`?(?P<hd>[0-9a-f]{7,40})`?)?%s"
        % (words, _TAIL), seg, re.IGNORECASE)
    if not m or not (m.group("st") or m.group("hd")):
        return None
    st = table[m.group("st").lower()] if m.group("st") else None
    return st, (m.group("hd").lower() if m.group("hd") else None)


def extract_claims(text: str, bead_prefixes: tuple[str, ...] = ("mk",),
                   default_repo: Optional[str] = None) -> list[Claim]:
    bead_re = re.compile(
        r"\b((?:%s)-[a-z0-9]{3,6}(?:\.\d+)*)\b" % "|".join(map(re.escape, bead_prefixes)))
    claims: list[Claim] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        refs: list[tuple[int, int, str, str]] = []  # start, end, kind, ref
        for m in _PR_URL_RE.finditer(line):
            refs.append((m.start(), m.end(), "pr", f"{m.group(1)}#{m.group(2)}"))
        for m in _PR_SLUG_RE.finditer(line):
            if not any(s <= m.start() < e for s, e, *_ in refs):
                refs.append((m.start(), m.end(), "pr", f"{m.group(1)}#{m.group(2)}"))
        for m in _PR_BARE_RE.finditer(line):
            if default_repo and not any(s <= m.start() < e for s, e, *_ in refs):
                refs.append((m.start(), m.end(), "pr", f"{default_repo}#{m.group(1)}"))
        for m in bead_re.finditer(line):
            refs.append((m.start(), m.end(), "bead", m.group(1)))
        for m in _CARD_RE.finditer(line):
            pre = m.group(1).split("-")[0]
            if pre in _CARD_STOP or pre.lower() in {p.lower() for p in bead_prefixes}:
                continue
            refs.append((m.start(), m.end(), "card", m.group(1)))
        refs.sort()
        short = line.strip()[:160]
        for s, e, kind, ref in refs:
            table = {"pr": _PR_STATES, "bead": _BEAD_STATES, "card": _CARD_STATES}[kind]
            parsed = None if _CONDITIONAL.search(line[max(0, s - 40):s]) else _explicit(line[e:], table)
            st, hd = parsed if parsed else (None, None)
            claims.append(Claim(kind, ref, lineno, st, short))
            if kind == "pr" and hd:
                claims.append(Claim("pr-head", ref, lineno, hd, short))
        # A hash claim is a line holding only a path and "sha256 HEX".
        hm = re.match(
            r"^[\s\-*>`]*(?:(?P<p1>(?:~|/)[\w./~+-]*\w)\s*[:=]?\s*)?sha256[\s:=`]*(?P<h>[0-9a-f]{12,64})`?"
            r"(?:\s*[:=]?\s*(?P<p2>(?:~|/)[\w./~+-]*\w))?[\s`.]*$", line, re.IGNORECASE)
        if hm and (hm.group("p1") or hm.group("p2")) and not (hm.group("p1") and hm.group("p2")):
            claims.append(Claim("hash", hm.group("p1") or hm.group("p2"), lineno,
                                hm.group("h").lower(), short))
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
                out.append(Result(c, STALE, str(actual)[:12], "note says " + (c.asserted or "")[:12]))
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
                r"(?<![\w-])%s(?![\w-])\s*[:(]\s*(open|waiting|pending)\b%s"
                % (re.escape(wid), _TAIL), line, re.IGNORECASE)
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
