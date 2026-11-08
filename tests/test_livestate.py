"""Tests for the handoff live-state list."""

from __future__ import annotations

import json
import os
import sys

import pytest

from intermem.livestate import (
    UNQUALIFIED, UNRESOLVED, Resolvers, extract_refs, render, resolve_refs,
    run_live_state, wait_refs, write_report,
)

R = "acme/widget"


def _res(pr=None, bead=None, card=None, thread=None, sha=None) -> Resolvers:
    return Resolvers(
        pr=lambda r, n: (pr or {}).get(f"{r}#{n}"),
        bead=lambda b: (bead or {}).get(b),
        card=lambda k: (card or {}).get(k),
        thread=lambda t: (thread or {}).get(t),
        file_sha256=lambda p: (sha or {}).get(p),
    )


def _kinds(text):
    return sorted((k, r) for k, r, _ in extract_refs(text))


def test_extracts_each_kind_once_per_line():
    text = (f"PR {R}#5 and https://github.com/{R}/pull/6, mk-abc12 mk-abc12, "
            "AUTA-517, thr_abcdefghij, /tmp/x.sh sha256 abc")
    assert _kinds(text) == sorted([
        ("pr", f"{R}#5"), ("pr", f"{R}#6"), ("bead", "mk-abc12"), ("card", "AUTA-517"),
        ("thread", "thr_abcdefghij"), ("file", "/tmp/x.sh")])


def test_bare_pr_is_unqualified_and_never_looked_up():
    called = []
    res = _res()
    res.pr = lambda r, n: called.append((r, n))
    out = resolve_refs(extract_refs("PR 128 is merged"), res)
    assert out[0].state == UNQUALIFIED and called == []


def test_slug_pr_not_also_listed_as_bare():
    assert _kinds(f"PR {R}#5") == [("pr", f"{R}#5")]


def test_sha256_and_utf8_are_not_cards():
    assert not [k for k in _kinds("uses SHA-256 and UTF-8 and mk-abc12") if k[0] == "card"]


def test_paths_only_on_hash_lines():
    assert _kinds("see /tmp/a.md") == []


def test_states_shown_and_failures_unresolved():
    res = _res(
        pr={f"{R}#5": {"state": "MERGED", "mergedAt": "x", "headRefOid": "a" * 40}},
        bead={"mk-abc12": {"status": "closed"}},
        card={"AUTA-517": {"status": "done"}},
        thread={"thr_abcdefghij": {"status": "idle", "archivedAt": 5}},
        sha={"/tmp/x": "f" * 64},
    )
    text = f"{R}#5 mk-abc12 AUTA-517 thr_abcdefghij mk-zzzzz /tmp/x sha256"
    got = {r.ref: r for r in resolve_refs(extract_refs(text), res)}
    assert got[f"{R}#5"].state == "merged" and got[f"{R}#5"].detail == "head " + "a" * 12
    assert got["mk-abc12"].state == "closed"
    assert got["AUTA-517"].state == "done"
    assert got["thr_abcdefghij"].state == "idle, archived"
    assert got["mk-zzzzz"].state == UNRESOLVED
    assert got["/tmp/x"].state == "sha256 " + "f" * 12


def test_lookup_cached_per_ref():
    n = []
    res = _res()
    res.bead = lambda b: n.append(b) or {"status": "open"}
    resolve_refs(extract_refs("mk-abc12\nmk-abc12 again"), res)
    assert n == ["mk-abc12"]


def test_waits_listed_by_id(tmp_path):
    wf = tmp_path / "waits.json"
    wf.write_text(json.dumps({"waits": [
        {"id": "wait-abcd", "kind": "pr", "ref": "x#1", "resolved_at": "t"},
        {"id": "wait-live", "kind": "time", "ref": "r", "resolved_at": None},
        {"id": "wait-gone"}]}))
    text = "keeps wait-abcd\nand wait-live.\nwait-abcdefg is other"
    got = {r.ref: r for r in wait_refs(text, wf)}
    assert got["wait-abcd"].state == "resolved t" and got["wait-abcd"].line == 1
    assert got["wait-live"].state == "open"
    assert "wait-gone" not in got and len(got) == 2


def test_bad_waits_file_is_empty(tmp_path):
    wf = tmp_path / "w.json"
    wf.write_text("not json")
    assert wait_refs("wait-abcd", wf) == []


def test_note_never_modified_and_no_verdict_words(tmp_path):
    note = tmp_path / "n.md"
    note.write_text(f"{R}#5: open\n")
    before = note.read_bytes()
    refs = run_live_state(note, _res(pr={f"{R}#5": {"state": "MERGED", "mergedAt": "x"}}))
    body = render(note, refs)
    assert note.read_bytes() == before
    assert "merged" in body and "STALE" not in body and "stale" not in body.lower()


def test_empty_note():
    assert "No references found" in render(__import__("pathlib").Path("n.md"), [])


def _cli(monkeypatch, note, out):
    from intermem import __main__ as m
    monkeypatch.setattr(sys, "argv", ["intermem", "live-state", str(note), "--out", str(out)])
    monkeypatch.setattr("intermem.livestate.default_resolvers",
                        lambda *_a, **_k: _res(pr={f"{R}#5": {"state": "OPEN"}}))
    with pytest.raises(SystemExit) as e:
        m.main()
    return e.value.code


def test_out_refuses_note_hardlink_symlink(tmp_path, monkeypatch):
    note = tmp_path / "n.md"
    note.write_text(f"{R}#5\n")
    link = tmp_path / "l.md"
    os.link(note, link)
    sym = tmp_path / "s.md"
    sym.symlink_to(note)
    for t in (note, link, sym):
        assert _cli(monkeypatch, note, t) == 2
    assert note.read_text() == f"{R}#5\n"


def test_out_replaces_alias_and_leaves_no_temp(tmp_path, monkeypatch):
    note = tmp_path / "n.md"
    note.write_text(f"{R}#5\n")
    other = tmp_path / "o.md"
    other.write_text("keep")
    os.link(other, tmp_path / "o2.md")
    assert _cli(monkeypatch, note, tmp_path / "o2.md") == 0
    assert other.read_text() == "keep"
    assert "open" in (tmp_path / "o2.md").read_text()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".ls-")]


def test_write_report_rejects_same_file(tmp_path):
    note = tmp_path / "n.md"
    note.write_text("x")
    with pytest.raises(ValueError):
        write_report(note, note, "y")
    assert note.read_text() == "x"


def test_missing_wait_and_bad_registry_are_unresolved(tmp_path):
    wid = "pr-acme-widget-5-0123abcd"
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps({"waits": []}))
    assert [(r.ref, r.state) for r in wait_refs(f"see {wid}", wf)] == [(wid, UNRESOLVED)]
    wf.write_text("not json")
    assert [(r.ref, r.state) for r in wait_refs(f"see {wid}", wf)] == [(wid, UNRESOLVED)]


def test_pr_url_requires_github_host():
    assert _kinds("https://notgithub.com/acme/widget/pull/12") == []
    assert _kinds("https://evil.github.com/acme/widget/pull/12") == []
    assert _kinds("https://evil.example/github.com/acme/widget/pull/12") == []
    assert _kinds("https://github.com/acme/widget/pull/12") == [("pr", "acme/widget#12")]
    assert _kinds("see github.com/acme/widget/pull/12.") == [("pr", "acme/widget#12")]


def test_file_hash_skips_devices_fifos_and_big_files(tmp_path, monkeypatch):
    from intermem import livestate
    h = livestate.default_resolvers().file_sha256
    assert h("/dev/zero") is None
    fifo = tmp_path / "f"
    os.mkfifo(fifo)
    assert h(str(fifo)) is None
    big = tmp_path / "b"
    big.write_bytes(b"x" * 10)
    monkeypatch.setattr(livestate, "_MAX_HASH_BYTES", 5)
    assert h(str(big)) is None
    ok = tmp_path / "ok"
    ok.write_bytes(b"abc")
    monkeypatch.setattr(livestate, "_MAX_HASH_BYTES", 100)
    assert h(str(ok)) == __import__("hashlib").sha256(b"abc").hexdigest()


def test_nested_url_and_relative_paths():
    assert _kinds("https://evil.example/https://github.com/acme/widget/pull/12") == []
    assert _kinds("sha256 ./a/b.md") == [("file", "./a/b.md")]
    assert _kinds("sha256 a/b.md") == []
    assert _kinds("sha256 ~/x /abs/y") == [("file", "/abs/y"), ("file", "~/x")]
