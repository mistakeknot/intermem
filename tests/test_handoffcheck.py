"""Tests for the read-only handoff stale-claim check."""

from __future__ import annotations

import hashlib
import json

from intermem.handoffcheck import (
    CURRENT, NOASSERT, STALE, UNRESOLVED, Resolvers, check_claims, check_waits,
    extract_claims, render_report, run_check,
)

R = "acme/widget"


def _res(pr=None, bead=None, card=None, sha=None) -> Resolvers:
    return Resolvers(
        pr=lambda r, n: (pr or {}).get(f"{r}#{n}"),
        bead=lambda b: (bead or {}).get(b),
        card=lambda k: (card or {}).get(k),
        file_sha256=lambda p: (sha or {}).get(p),
    )


def _verdicts(text, res, **kw):
    claims = extract_claims(text, default_repo=kw.get("default_repo"))
    return check_claims(claims, res)


def test_pr_merged_claimed_open_is_stale():
    res = _res(pr={f"{R}#5": {"state": "MERGED", "mergedAt": "2026-11-06", "headRefOid": "abc1234def"}})
    out = _verdicts("PR acme/widget#5 is open and waiting", res)
    assert out[0].verdict == STALE and out[0].actual == "merged"


def test_pr_state_matches_is_current():
    res = _res(pr={f"{R}#5": {"state": "OPEN", "mergedAt": None, "headRefOid": "abc"}})
    assert _verdicts("acme/widget#5 open", res)[0].verdict == CURRENT


def test_bare_pr_needs_default_repo():
    text = "PR 7 merged"
    assert extract_claims(text) == []
    res = _res(pr={f"{R}#7": {"state": "MERGED", "mergedAt": "x"}})
    assert _verdicts(text, res, default_repo=R)[0].verdict == CURRENT


def test_no_assertion_is_noassert_not_stale():
    res = _res(pr={f"{R}#5": {"state": "MERGED", "mergedAt": "x"}})
    assert _verdicts("see acme/widget#5 for details", res)[0].verdict == NOASSERT


def test_conditional_and_negated_not_asserted():
    res = _res(pr={f"{R}#5": {"state": "MERGED", "mergedAt": "x"}})
    assert _verdicts("wait until acme/widget#5 merged", res)[0].verdict == NOASSERT
    assert _verdicts("acme/widget#5 not merged yet", res)[0].verdict == NOASSERT


def test_pr_head_mismatch():
    res = _res(pr={f"{R}#5": {"state": "OPEN", "headRefOid": "ffff0000aaaa1111"}})
    out = _verdicts("acme/widget#5 open, head 1234567", res)
    kinds = {r.claim.kind: r.verdict for r in out}
    assert kinds == {"pr": CURRENT, "pr-head": STALE}


def test_bead_closed_vs_open():
    res = _res(bead={"mk-abc12": {"status": "closed"}})
    assert _verdicts("mk-abc12 is open", res)[0].verdict == STALE
    assert _verdicts("mk-abc12 closed", res)[0].verdict == CURRENT


def test_card_done_claimed_todo():
    res = _res(card={"AUTA-517": {"status": "done"}})
    out = _verdicts("AUTA-517 todo, waiting on mk", res)
    assert out[0].verdict == STALE and out[0].actual == "done"


def test_unresolved_is_not_stale():
    out = _verdicts("mk-zzzzz open", _res())
    assert out[0].verdict == UNRESOLVED


def test_hash_mismatch_and_match(tmp_path):
    f = tmp_path / "deploy.sh"
    f.write_bytes(b"hello")
    real = hashlib.sha256(b"hello").hexdigest()
    res = _res(sha={str(f): real})
    ok = _verdicts(f"script {f} sha256 {real[:16]}", res)
    assert ok[0].verdict == CURRENT
    bad = _verdicts(f"script {f} sha256 {'0' * 16}", res)
    assert bad[0].verdict == STALE


def test_waits_resolved_but_called_open(tmp_path):
    wf = tmp_path / "waits.json"
    wf.write_text(json.dumps({"v": 1, "waits": [
        {"id": "wait-pr-245", "kind": "pr", "ref": "x", "resolved_at": "2026-11-07"},
        {"id": "wait-live", "kind": "pr", "ref": "y", "resolved_at": None}]}))
    text = "- wait-pr-245 still open\n- wait-live open\n"
    out = check_waits(text, wf)
    assert len(out) == 1 and out[0].claim.ref == "wait-pr-245" and out[0].verdict == STALE


def test_run_check_never_modifies_note(tmp_path):
    note = tmp_path / "handoff.md"
    note.write_text("acme/widget#5 open\n")
    before = note.read_bytes()
    res = _res(pr={f"{R}#5": {"state": "MERGED", "mergedAt": "x"}})
    results = run_check(note, res)
    report = render_report(note, results)
    assert "Stale" in report and "acme/widget#5" in report
    assert note.read_bytes() == before


def test_sha_not_confused_for_card():
    assert not [c for c in extract_claims("uses SHA-256 and UTF-8") if c.kind == "card"]


def test_imperative_and_conditional_are_not_assertions():
    res = _res(pr={f"{R}#5": {"state": "MERGED", "mergedAt": "x", "headRefOid": "aaaaaaa1"}})
    for t in ("acme/widget#5, open the dashboard", "acme/widget#5 if open, investigate",
              "acme/widget#5 not head 1234567"):
        assert all(r.verdict in (NOASSERT, UNRESOLVED) or r.claim.kind == "pr" and r.verdict == NOASSERT
                   for r in _verdicts(t, res)), t
        assert not [r for r in _verdicts(t, res) if r.verdict == STALE], t


def test_wait_negation_and_clauses(tmp_path):
    wf = tmp_path / "waits.json"
    wf.write_text(json.dumps({"waits": [{"id": "wait-abcd", "resolved_at": "t"}]}))
    for t in ("wait-abcd is not open", "wait-abcd resolved; PR 5 open",
              "if wait-abcd is open, check later"):
        assert check_waits(t, wf) == [], t
    assert len(check_waits("wait-abcd is open", wf)) == 1


def test_hash_binds_to_nearest_path_or_none():
    a = "a" * 64
    one = extract_claims(f"/tmp/first /tmp/second sha256 {a}")
    assert one[0].ref == "/tmp/second"
    amb = extract_claims(f"/tmp/a sha256 {a} and /tmp/b")
    assert amb[0].ref in ("/tmp/a", "")  # ambiguous allowed to be unbound
    tie = extract_claims(f"/tmp/aaaa sha256 {a} /tmp/bbbb")
    assert tie[0].ref == ""


def test_out_cannot_be_hardlink_of_note(tmp_path, monkeypatch, capsys):
    import os, sys
    from intermem import __main__ as m
    note = tmp_path / "n.md"
    note.write_text("acme/widget#5 open\n")
    link = tmp_path / "l.md"
    os.link(note, link)
    monkeypatch.setattr(sys, "argv", ["intermem", "handoff-check", str(note), "--out", str(link)])
    monkeypatch.setattr("intermem.handoffcheck.default_resolvers",
                        lambda *_a, **_k: _res(pr={f"{R}#5": {"state": "OPEN"}}))
    try:
        m.main()
    except SystemExit as e:
        assert e.code == 2
    assert note.read_text() == "acme/widget#5 open\n"
