"""Tests for the read-only handoff stale-claim check (explicit forms only)."""

from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

from intermem.handoffcheck import (
    CURRENT, NOASSERT, STALE, UNRESOLVED, Resolvers, check_claims, check_waits,
    extract_claims, render_report, run_check,
)

R = "acme/widget"
MERGED = {"state": "MERGED", "mergedAt": "x", "headRefOid": "aaaaaaa1bbbb"}


def _res(pr=None, bead=None, card=None, sha=None) -> Resolvers:
    return Resolvers(
        pr=lambda r, n: (pr or {}).get(f"{r}#{n}"),
        bead=lambda b: (bead or {}).get(b),
        card=lambda k: (card or {}).get(k),
        file_sha256=lambda p: (sha or {}).get(p),
    )


def _v(text, res, repo=None):
    return check_claims(extract_claims(text, default_repo=repo), res)


# ---- explicit forms are checked ----------------------------------------

@pytest.mark.parametrize("text", [
    f"{R}#5: open", f"{R}#5 (open)", f"- {R}#5: open.", f"{R}#5: open; next item",
])
def test_pr_explicit_forms_flag_stale(text):
    out = _v(text, _res(pr={f"{R}#5": MERGED}))
    assert out[0].verdict == STALE and out[0].actual == "merged"


def test_pr_state_matches_is_current():
    assert _v(f"{R}#5: merged", _res(pr={f"{R}#5": MERGED}))[0].verdict == CURRENT


def test_bare_pr_needs_default_repo():
    assert extract_claims("PR 7: merged") == []
    assert _v("PR 7: merged", _res(pr={f"{R}#7": MERGED}), repo=R)[0].verdict == CURRENT


def test_pr_head_forms():
    res = _res(pr={f"{R}#5": {"state": "OPEN", "headRefOid": "ffff0000aaaa1111"}})
    kinds = {r.claim.kind: r.verdict for r in _v(f"{R}#5 (open, head 1234567)", res)}
    assert kinds == {"pr": CURRENT, "pr-head": STALE}
    kinds = {r.claim.kind: r.verdict for r in _v(f"{R}#5: head ffff000", res)}
    assert kinds == {"pr": NOASSERT, "pr-head": CURRENT}


def test_bead_and_card_explicit():
    assert _v("mk-abc12 (open)", _res(bead={"mk-abc12": {"status": "closed"}}))[0].verdict == STALE
    assert _v("mk-abc12: closed", _res(bead={"mk-abc12": {"status": "closed"}}))[0].verdict == CURRENT
    out = _v("AUTA-517: todo", _res(card={"AUTA-517": {"status": "done"}}))
    assert out[0].verdict == STALE and out[0].actual == "done"


def test_unresolved_is_not_stale():
    assert _v("mk-zzzzz (open)", _res())[0].verdict == UNRESOLVED


def test_hash_line_only(tmp_path):
    f = tmp_path / "deploy.sh"
    f.write_bytes(b"hello")
    real = hashlib.sha256(b"hello").hexdigest()
    res = _res(sha={str(f): real})
    assert _v(f"{f} sha256 {real[:16]}", res)[0].verdict == CURRENT
    assert _v(f"- {f}: sha256 `{'0' * 16}`", res)[0].verdict == STALE
    assert _v(f"sha256 {real[:16]} {f}", res)[0].verdict == CURRENT


# ---- everything else is NOASSERT / not extracted ------------------------

@pytest.mark.parametrize("text", [
    f"{R}#5, open the dashboard",
    f"{R}#5 if open, investigate",
    f"{R}#5 open or closed?",
    f"{R}#5 open?",
    f"{R}#5: open or closed",
    f"{R}#5 open, if CI passes",
    f"{R}#5: open, unless it merged",
    f"{R}#5: open once CI passes",
    f"{R}#5: open until review",
    f"{R}#5 is open",
    f"{R}#5 open and waiting",
    f"{R}#5 not head 1234567",
    f"{R}#5 open and the deployment head 1234567",
    f"{R}#5 head abcdef0?",
    f"wait until {R}#5: merged",
    f"{R}#5 not: merged",
])
def test_non_explicit_text_never_asserts(text):
    out = _v(text, _res(pr={f"{R}#5": MERGED}))
    assert out and not [r for r in out if r.verdict == STALE]
    assert not [r for r in out if r.claim.asserted]


@pytest.mark.parametrize("text", [
    "/tmp/a sha256 " + "a" * 64 + "?",
    "/tmp/a sha256 " + "a" * 12 + " or " + "b" * 12,
    "if /tmp/a sha256 " + "a" * 64,
    "/tmp/a sha256 " + "a" * 64 + " is incorrect",
    "/tmp/first /tmp/second sha256 " + "a" * 64,
    "/tmp/a sha256 " + "a" * 64 + " /tmp/b",
    "the deploy changed: /tmp/a sha256 " + "a" * 64,
    "sha256 " + "a" * 64,
])
def test_hash_prose_not_extracted(text):
    assert extract_claims(text) == []


def test_waits(tmp_path):
    wf = tmp_path / "waits.json"
    wf.write_text(json.dumps({"waits": [{"id": "wait-abcd", "resolved_at": "t"},
                                         {"id": "wait-live", "resolved_at": None}]}))
    for t in ("wait-abcd is not open", "wait-abcd resolved; PR 5 open",
              "if wait-abcd is open, check later", "wait-abcd open, or resolved?",
              "wait-abcd: open, if CI passes", "wait-abcd is open", "wait-live: open"):
        assert check_waits(t, wf) == [], t
    for t in ("wait-abcd: open", "- wait-abcd (open)", "wait-abcd: pending."):
        out = check_waits(t, wf)
        assert len(out) == 1 and out[0].verdict == STALE, t


# ---- read-only guarantees ----------------------------------------------

def test_run_check_never_modifies_note(tmp_path):
    note = tmp_path / "handoff.md"
    note.write_text(f"{R}#5: open\n")
    before = note.read_bytes()
    results = run_check(note, _res(pr={f"{R}#5": MERGED}))
    assert f"{R}#5" in render_report(note, results)
    assert note.read_bytes() == before


def test_sha_not_confused_for_card():
    assert not [c for c in extract_claims("uses SHA-256 and UTF-8") if c.kind == "card"]


def _run_cli(monkeypatch, note, out):
    from intermem import __main__ as m
    monkeypatch.setattr(sys, "argv", ["intermem", "handoff-check", str(note), "--out", str(out)])
    monkeypatch.setattr("intermem.handoffcheck.default_resolvers",
                        lambda *_a, **_k: _res(pr={f"{R}#5": {"state": "OPEN"}}))
    with pytest.raises(SystemExit) as e:
        m.main()
    return e.value.code


def test_out_refuses_hardlink_symlink_and_self(tmp_path, monkeypatch):
    note = tmp_path / "n.md"
    note.write_text(f"{R}#5: open\n")
    link = tmp_path / "l.md"
    os.link(note, link)
    sym = tmp_path / "s.md"
    sym.symlink_to(note)
    for target in (note, link, sym):
        assert _run_cli(monkeypatch, note, target) == 2
    assert note.read_text() == f"{R}#5: open\n"


def test_out_replaces_never_truncates_and_ignores_predictable_tmp(tmp_path, monkeypatch):
    out = tmp_path / "report"
    note = tmp_path / f"report.tmp{os.getpid()}"
    note.write_text(f"{R}#5: open\n")
    _run_cli(monkeypatch, note, out)
    assert note.read_text() == f"{R}#5: open\n"
    assert "Stale-claim check" in out.read_text()
    other = tmp_path / "o.md"
    other.write_text("x")
    os.link(other, tmp_path / "o2.md")
    _run_cli(monkeypatch, note, tmp_path / "o2.md")
    assert other.read_text() == "x"
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".hc-")]
