"""
Pure-logic tests for tools/sync_engine.py.

These need no Studio, no MCP and no network, so they can always run -- which
matters because this module holds the rules that decide whether user code gets
overwritten.  Run directly:

    python tests/test_sync_engine.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import sync_engine as se  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, (" -- " + detail) if detail else ""))


def h(text: str) -> str:
    return se.sha256(text)


def studio(sha: str, fnv: str = "f0", cls: str = "Script") -> dict:
    return {"className": cls, "sha256": sha, "fnv": fnv, "bytes": 10}


def local(sha: str) -> dict:
    return {"sha256": sha, "bytes": 10, "mtime": 0.0}


def main() -> int:
    print("=" * 74)
    print("sync_engine unit tests")
    print("=" * 74)

    print("\n-- 1. studio path <-> local path mapping --")
    cases = [
        ("ServerScriptService.Foo", "Script", "ServerScriptService/Foo.server.luau"),
        ("ReplicatedStorage.Bar", "ModuleScript", "ReplicatedStorage/Bar.luau"),
        ("StarterPlayer.StarterPlayerScripts.Baz", "LocalScript",
         "StarterPlayer/StarterPlayerScripts/Baz.client.luau"),
    ]
    for studio_path, cls, rel in cases:
        check("map %s -> %s" % (studio_path, rel), se.rel_path_for(studio_path, cls) == rel)
        check("inverse of %s" % rel, se.parse_local_path(rel) == (studio_path, cls))

    check("non-luau file is not a script", se.parse_local_path("notes/readme.md") is None)
    check("plain .luau defaults to ModuleScript",
          se.parse_local_path("ReplicatedStorage/Plain.luau") == ("ReplicatedStorage.Plain", "ModuleScript"))

    print("\n-- 2. CRLF canonicalisation prevents phantom edits --")
    check("CRLF and LF hash identically", h("a\r\nb\r\n") == h("a\nb\n"))
    check("lone CR normalises too", se.canonical("a\rb") == "a\nb")
    check("different content still differs", h("a") != h("b"))

    print("\n-- 3. decision table --")
    S, L, B = h("studio"), h("local"), h("old")

    d = se.decide("A.B", None, "A/B.server.luau", None, None)
    check("absent both sides -> nothing", d.kind == "nothing", d.kind)

    d = se.decide("A.B", studio(S, cls="Script"), "A/B.server.luau", None, None)
    check("new in studio -> pull", d.kind == "pull", d.kind)

    d = se.decide("A.B", studio(S), "A/B.server.luau", None, {"sha256": B})
    check("studio present, local deleted -> local-missing (no studio delete)",
          d.kind == "local-missing", d.kind)

    d = se.decide("A.B", None, "A/B.server.luau", local(L), None)
    check("local only, no baseline -> create", d.kind == "create", d.kind)

    d = se.decide("A.B", None, "A/B.server.luau", local(L), {"sha256": B})
    check("vanished from studio -> studio-missing (no local delete)",
          d.kind == "studio-missing", d.kind)

    d = se.decide("A.B", studio(S), "A/B.server.luau", local(S), {"sha256": S})
    check("identical + baseline -> converged", d.kind == "converged", d.kind)
    check("converged is not flagged as adopt", not d.detail.get("adopt"))

    d = se.decide("A.B", studio(S), "A/B.server.luau", local(S), {"sha256": B})
    check("identical, baseline stale -> converged(adopt)", d.kind == "converged" and d.detail.get("adopt"))

    d = se.decide("A.B", studio(S), "A/B.server.luau", local(B), {"sha256": B})
    check("studio changed only -> pull", d.kind == "pull", d.kind)

    d = se.decide("A.B", studio(B), "A/B.server.luau", local(L), {"sha256": B})
    check("local changed only -> push", d.kind == "push", d.kind)

    d = se.decide("A.B", studio(S), "A/B.server.luau", local(L), {"sha256": B})
    check("both changed -> conflict", d.kind == "conflict", d.kind)

    d = se.decide("A.B", studio(S), "A/B.server.luau", local(L), None)
    check("both exist, no baseline, different -> conflict", d.kind == "conflict", d.kind)

    print("\n-- 4. loop prevention: a completed sync must settle --")
    # simulate pull then re-decide
    state = se.SyncState(path=Path("unused.json"))
    state.record("A.B", class_name="Script", rel="A/B.server.luau",
                 content_hash=S, studio_fnv="f0", direction="pull")
    d = se.decide("A.B", studio(S, "f0"), "A/B.server.luau", local(S), state.get("A.B"))
    check("after a pull the next cycle is converged", d.kind == "converged", d.kind)

    # simulate push then re-decide (the echo-suppression case)
    state2 = se.SyncState(path=Path("unused.json"))
    state2.record("A.B", class_name="Script", rel="A/B.server.luau",
                  content_hash=L, studio_fnv="f9", direction="push")
    d = se.decide("A.B", studio(L, "f9"), "A/B.server.luau", local(L), state2.get("A.B"))
    check("after a push the next cycle is converged (no echo)", d.kind == "converged", d.kind)

    print("\n-- 5. deletions never propagate --")
    dec = se.decide("A.B", None, "A/B.server.luau", local(L), {"sha256": B})
    check("studio deletion does not delete the local file", dec.kind != "push" and dec.kind != "delete")
    dec = se.decide("A.B", studio(S), "A/B.server.luau", None, {"sha256": B})
    check("local deletion does not delete the studio script", dec.kind != "delete")

    print("\n-- 6. rename detection --")
    content = h("same content")
    state3 = se.SyncState(path=Path("unused.json"))
    state3.record("ServerScriptService.Old", class_name="Script",
                  rel="ServerScriptService/Old.server.luau", content_hash=content,
                  studio_fnv="aa", direction="pull")
    snap = {
        "ServerScriptService.New": {
            "className": "Script", "sha256": content, "fnv": "aa",
            "rel": "ServerScriptService/New.server.luau",
        }
    }
    local_idx = {"ServerScriptService/Old.server.luau": local(content)}
    decisions = se.plan(snap, local_idx, state3)
    kinds = {d.kind: d for d in decisions}
    check("rename detected as a single rename action", "rename" in kinds, str(sorted(kinds)))
    if "rename" in kinds:
        r = kinds["rename"]
        check("rename moves Old -> New",
              r.detail["fromRel"] == "ServerScriptService/Old.server.luau"
              and r.detail["toRel"] == "ServerScriptService/New.server.luau")
        check("rename keeps the content hash", r.detail["sha256"] == content)

    # ambiguity must NOT be auto-renamed
    snap2 = {
        "A.New1": {"className": "Script", "sha256": content, "fnv": "aa", "rel": "A/New1.server.luau"},
        "A.New2": {"className": "Script", "sha256": content, "fnv": "ab", "rel": "A/New2.server.luau"},
    }
    dec2 = se.plan(snap2, {"A/Old.server.luau": local(content)}, state3)
    check("ambiguous rename is left alone", not any(d.kind == "rename" for d in dec2),
          str(sorted({d.kind for d in dec2})))

    # a genuinely deleted script (no content match) is not renamed
    snap3 = {"A.Other": {"className": "Script", "sha256": h("different"),
                         "fnv": "ac", "rel": "A/Other.server.luau"}}
    dec3 = se.plan(snap3, {"A/Old.server.luau": local(content)}, state3)
    check("unmatched deletion is not renamed", not any(d.kind == "rename" for d in dec3))

    print("\n-- 7. local_index ignores noise, hashes content --")
    tmp = Path(tempfile.mkdtemp(prefix="rss-test-"))
    try:
        src = tmp / "src"
        (src / "ServerScriptService").mkdir(parents=True)
        (src / "ServerScriptService" / "Keep.server.luau").write_text("print(1)\n", encoding="utf-8")
        (src / "ServerScriptService" / "Keep.conflict-20260101.server.luau").write_text("junk", encoding="utf-8")
        (src / "ServerScriptService" / "notes.md").write_text("nope", encoding="utf-8")
        (src / "ServerScriptService" / "backup.bak").write_text("nope", encoding="utf-8")
        (src / ".hidden").mkdir()
        (src / ".hidden" / "Hidden.server.luau").write_text("x", encoding="utf-8")

        index = se.local_index(src)
        check("keeps the real script", "ServerScriptService/Keep.server.luau" in index, str(list(index)))
        check("ignores conflict files",
              not any("conflict" in k for k in index), str(list(index)))
        check("ignores non-luau files", not any(k.endswith(".md") for k in index))
        check("ignores .bak files", not any(k.endswith(".bak") for k in index))
        check("ignores dot-directories", not any(".hidden" in k for k in index))
        check("indexed content is canonical LF",
              index["ServerScriptService/Keep.server.luau"]["sha256"] == h("print(1)\n"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n-- 8. SyncState round trip --")
    tmp = Path(tempfile.mkdtemp(prefix="rss-state-"))
    try:
        sp = tmp / "sync-state.json"
        st = se.SyncState.load(sp)
        check("missing state file loads empty", st.entries == {})
        st.place_id = 12345
        st.record("A.B", class_name="Script", rel="A/B.server.luau",
                  content_hash=h("x"), studio_fnv="ff", direction="pull")
        st.mark("A.C", "conflict", "both changed")
        st.save()

        st2 = se.SyncState.load(sp)
        check("entries survive the round trip", st2.get("A.B")["sha256"] == h("x"))
        check("place id survives", st2.place_id == 12345)
        check("unresolved survives", "A.C" in st2.unresolved)
        check("baseline helper works", st2.baseline("A.B") == h("x"))
        check("no temp file left behind", not (tmp / "sync-state.json.tmp").exists())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n-- 9. summarise --")
    decs = [
        se.Decision("pull", "A", "A.luau", "Script", "x"),
        se.Decision("pull", "B", "B.luau", "Script", "x"),
        se.Decision("conflict", "C", "C.luau", "Script", "x"),
    ]
    check("counts by kind", se.summarise(decs) == {"conflict": 1, "pull": 2}, str(se.summarise(decs)))

    print("\n" + "=" * 74)
    print("PASSED %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print("  FAILED:", f)
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
