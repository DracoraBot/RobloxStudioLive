"""
Self-test for tools/studio_ops.py -- run directly:

    python tests/selftest_studio_ops.py

Non-destructive by construction:
  * the write test writes content byte-identical to what is already in Studio
    (a no-op change), proving the write path without altering the game;
  * the create test makes one scratch ModuleScript under ServerStorage and
    removes that same instance afterwards, leaving the place exactly as found.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import studio_ops as ops  # noqa: E402
from mcp_client import StudioMCPClient  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, (" -- " + detail) if detail else ""))


def main() -> int:
    print("=" * 74)
    print("studio_ops self-test")
    print("=" * 74)

    with StudioMCPClient() as client:
        def roundtrip(text: str) -> str:
            """Send text into Luau as a literal and read it straight back."""
            got = client.execute_luau("return {S = " + ops.lua_string_literal(text) + "}")
            return got.get("S") if isinstance(got, dict) else None

        print("\n-- 1. literal escaping is byte-exact --")
        tricky = (
            "local s = [[nested]] .. ']=]' .. [==[x]==]\n"
            "print(string.format('%d%% of {a=1}', 50))\n"
            "local bs = 'C:\\\\path\\\\to\\\\file'\n"
            "local q = \"quote ' and \\\"double\\\"\"\n"
            "crlf\r\nline\rmac\rline\n"
            + ("payload " * 300)
            + "\n--[==[ trailing ]==]\n"
        )
        check("mixed brackets/%%/braces/CRLF round-trip", roundtrip(tricky) == tricky,
              "%d bytes" % len(tricky))

        for label, text in [
            ("]]", "a]]b"), ("]=]", "a]=]b"), ("]==]", "a]==]b"),
            ("[==", "[==["), ("empty", ""), ("only cr", "\r"),
            ("crlf only", "\r\n"), ("unicode", "héllo — 日本語 🎮"),
            ("nul-adjacent ctrl", "a\x01b\x7fc"),
        ]:
            check("escape case %s" % label, roundtrip(text) == text)

        print("\n-- 2. lua_value emits Luau literals (not JSON) --")
        payload = {
            "list": ["a", "b"],
            "nested": {"n": 3, "flag": True},
            "weird key": "has space",
            "end": "keyword key",
            "bracket]]key": "needs care",
            "pct": "100% {x}",
        }
        # Same shape the production reads use: build a Luau table, hand back a
        # JSON string via HttpService:JSONEncode.
        encode = "return game:GetService('HttpService'):JSONEncode({V = %s})"
        got = client.execute_luau(encode % ops.lua_value(payload))
        check("lua_value round-trips a nested structure",
              got == {"V": payload}, str(got)[:200])
        absent = client.execute_luau(encode % ops.lua_value({"k": None, "n": 1}))
        check("None becomes an absent key (not a null)",
              absent == {"V": {"n": 1}}, str(absent)[:120])

        print("\n-- 2b. oversized responses fail loudly, never silently --")
        from mcp_client import StudioMCPError
        try:
            client.execute_luau(
                "local t = {blob = string.rep('z', 300000)}\n"
                "return game:GetService('HttpService'):JSONEncode(t)"
            )
            check("300 KB single response is detected as truncation", False, "no error raised")
        except StudioMCPError as exc:
            check("300 KB single response is detected as truncation",
                  "truncat" in str(exc).lower() or "invalid JSON" in str(exc),
                  str(exc).splitlines()[-1][:70])

        print("\n-- 3. snapshot poll (cost of one sync cycle) --")
        t0 = time.time()
        snap = ops.snapshot_scripts(client)
        dt = time.time() - t0
        total = sum(s.get("bytes", 0) for s in snap.values())
        check("snapshot returns a path-keyed mapping", isinstance(snap, dict) and len(snap) > 0,
              "%d scripts, %d bytes of source" % (len(snap), total))
        check("snapshot carries no source text",
              all("source" not in s for s in snap.values()))
        print("      poll round-trip: %.0f ms for %d scripts" % (dt * 1000, len(snap)))

        biggest_path, biggest = max(snap.items(), key=lambda kv: kv[1].get("bytes", 0))
        print("      largest: %s (%d bytes, %s)" % (biggest_path, biggest["bytes"], biggest["className"]))

        print("\n-- 4. fetch full source + authoritative hash --")
        sizes = {p: m["bytes"] for p, m in snap.items()}
        t0 = time.time()
        sources = ops.fetch_sources(client, list(snap), sizes=sizes)
        dt = time.time() - t0
        check("fetched every script (1.4 MB, sliced)", len(sources) == len(snap),
              "%d/%d in %.0f ms" % (len(sources), len(snap), dt * 1000))
        bad = [p for p, s in sources.items()
               if len(s.encode("utf-8")) != snap[p]["bytes"]]
        check("byte counts match the snapshot for all scripts", not bad, str(bad[:3]))

        # the oversized one is the interesting case: it cannot fit in one response
        big_src = ops.fetch_sources(client, [biggest_path], sizes={biggest_path: biggest["bytes"]})
        check("single %d-byte script survives slicing intact" % biggest["bytes"],
              big_src.get(biggest_path) == sources[biggest_path])
        print("      sha256(%s) = %s..." % (biggest["name"],
                                            hashlib.sha256(sources[biggest_path].encode()).hexdigest()[:20]))

        print("\n-- 5. independent re-hash agrees --")
        ver = ops.verify_sources(client, [{"path": p, "expected_fnv": m["fnv"]} for p, m in snap.items()])
        mism = [p for p, v in ver.items() if not v.get("match")]
        check("verify_sources reproduces every snapshot hash", not mism, str(mism[:3]))

        print("\n-- 6. compare-and-swap write (identical content, no-op change) --")
        target = biggest_path
        ident = sources[target]
        res = ops.write_sources(client, [{
            "path": target, "source": ident, "expected_fnv": snap[target]["fnv"],
        }])
        entry = res.get(target, {})
        check("write reported success", entry.get("ok"), str(entry.get("status")))
        check("hash unchanged after identical write",
              entry.get("newFnv") == snap[target]["fnv"],
              "%s vs %s" % (entry.get("newFnv"), snap[target]["fnv"]))

        print("\n-- 7. conflict guard: stale expected hash is refused --")
        res = ops.write_sources(client, [{
            "path": target, "source": "-- must never be applied\n", "expected_fnv": "00000000:0",
        }])
        check("stale hash refused as conflict",
              res.get(target, {}).get("status") == "conflict", str(res.get(target, {}).get("status")))
        after = ops.fetch_sources(client, [target])[target]
        check("refused write left Studio content untouched", after == ident)

        print("\n-- 8. multi-script batch write --")
        trio = list(snap)[:3]
        res = ops.write_sources(client, [
            {"path": p, "source": sources[p], "expected_fnv": snap[p]["fnv"]} for p in trio
        ])
        check("batch of %d written" % len(trio),
              all(res.get(p, {}).get("ok") for p in trio), str({p: res.get(p, {}).get("status") for p in trio}))

        print("\n-- 9. create / duplicate-refusal / guarded delete --")
        parent, name = "ServerStorage", "RSS_SelfTest_Scratch"
        full = parent + "." + name
        if ops.script_exists(client, full):
            ops.delete_script(client, full, ops.DELETE_CONFIRM_TOKEN)
        body = "-- scratch\nreturn { greeting = 'hi', pct = string.format('%d%%', 7) }\n"
        created = ops.create_script(client, parent, name, "ModuleScript", body)
        check("create_script succeeded", created.get("ok"), str(created.get("status")))
        check("create_script refuses duplicates",
              ops.create_script(client, parent, name, "ModuleScript", body).get("status") == "exists")
        check("created content is byte-exact", ops.fetch_sources(client, [full]).get(full) == body)
        check("delete refused without exact token",
              ops.delete_script(client, full, "yes").get("status") == "refused")
        check("scratch survives the refused delete", ops.script_exists(client, full))
        check("scratch removed with token",
              ops.delete_script(client, full, ops.DELETE_CONFIRM_TOKEN).get("ok"))
        check("scratch is gone", not ops.script_exists(client, full))

        print("\n-- 10. final integrity re-check --")
        snap2 = ops.snapshot_scripts(client)
        check("script count unchanged", len(snap2) == len(snap), "%d vs %d" % (len(snap2), len(snap)))
        changed = [p for p in snap2 if p in snap and snap2[p]["fnv"] != snap[p]["fnv"]]
        check("no live script content was modified", not changed, str(changed[:5]))

        print("\n-- 11. instance tree --")
        tree = ops.instance_tree(client, "ServerScriptService", max_depth=2)
        check("instance_tree returns a mapping", isinstance(tree, dict) and len(tree) > 0,
              "%d nodes" % len(tree))

    print("\n" + "=" * 74)
    print("PASSED %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print("  FAILED:", f)
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
