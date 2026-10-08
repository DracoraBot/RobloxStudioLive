"""
Codegen edge-case tests.

These cover defects found by independent verifiers plus two found while fixing
them:

  * the change-detector hash claimed to be 32-bit FNV-1a but was not, because
    `h * 16777619` reaches ~2^56 and Roblox's bit32 returns IEEE doubles, so
    low bits were rounded away (66/66 real scripts disagreed with real FNV-1a);
  * `_render()`'s completeness check scanned the *substituted output*, so any
    script containing something like `@@HELLO@@` could never be synced;
  * `lua_quote` emitted `\\9` for tab, which merges with a following digit into
    a different escape (`\\95` = "_");
  * `lua_long_string` closed early when the text ended with `]`, because
    `text + "]]"` ends in `]]]` -- reachable for any script with no trailing
    newline whose last line ends in `]`.

Everything is proved by a live round trip through the bridge rather than by
inspecting the generated text.

    python tests/test_codegen_edges.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import studio_ops as ops  # noqa: E402
from mcp_client import StudioMCPClient, StudioMCPError  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, (" -- " + detail) if detail else ""))


def py_fnv1a_32(data: bytes) -> str:
    """A plain, independent 32-bit FNV-1a.  Python ints are exact."""
    h = 2166136261
    for byte in data:
        h = ((h ^ byte) * 16777619) % 4294967296
    return "%08x" % h


def main() -> int:
    print("=" * 74)
    print("codegen edge-case tests")
    print("=" * 74)

    print("\n-- 1. _render tolerates token-shaped text inside a script body --")
    body = 'local x = "@@HELLO@@" .. "@@BODY@@" .. "@@A1@@"'
    try:
        rendered = ops._render("local BODY = @@BODY@@\nreturn BODY",
                               BODY=ops.lua_string_literal(body))
        check("script containing @@TOKEN@@ renders", "@@HELLO@@" in rendered)
    except ValueError as exc:
        check("script containing @@TOKEN@@ renders", False, str(exc))

    try:
        ops._render("local A = @@MISSING@@", PRESENT="1")
        check("a genuinely unbound template token still raises", False, "no error raised")
    except ValueError as exc:
        check("a genuinely unbound template token still raises", True, str(exc))

    with StudioMCPClient() as client:
        def roundtrip(text: str) -> bool:
            got = client.execute_luau("return {S = %s}" % ops.lua_string_literal(text))
            return isinstance(got, dict) and got.get("S") == text

        print("\n-- 2. text ending in ']' must not close the literal early --")
        for label, text in [
            ("ends with ]", "local x = t[1]"),
            ("ends with ]]", "local x = a[b[1]]"),
            ("ends with ]]]", "x[1][2][3]]]"),
            ("just ]", "]"),
            ("just ]]", "]]"),
            ("]=] at end", "y = z]=]"),
            ("newline then ]", "return t[1]\n]"),
            ("] with trailing newline", "return t[1]\n"),
        ]:
            check("round-trip %s" % label, roundtrip(text), repr(text[:24]))

        print("\n-- 3. control-character escapes cannot merge with a digit --")
        for label, text in [("tab+5", "\t5"), ("soh+7", "\x017"), ("del+9", "\x7f9"),
                            ("esc+0", "\x1b0"), ("mixed", "a\t5\x017"),
                            ("stx+0", "\x020")]:
            check("round-trip %s" % label, roundtrip(text), repr(text))

        # A NUL byte cannot survive the bridge's own command parser.  Roblox
        # Source is text and never contains one, but if it ever did the write
        # must fail loudly rather than silently truncate.
        nul_rejected = False
        try:
            roundtrip("a\x00b")
        except StudioMCPError:
            nul_rejected = True
        check("a NUL byte is rejected loudly, not silently mangled", nul_rejected)

        print("\n-- 4. FNV-1a is exact against an independent Python implementation --")
        samples = [
            "", "a", "abc", "hello world", "The quick brown fox jumps over the lazy dog",
            "x" * 1000, "héllo", "日本語", "🎮 emoji",
            "".join(chr(i) for i in range(32, 127)),
            "-- Tower\nreturn { a = 1 }\n" * 50,
        ]
        matched = 0
        for sample in samples:
            code = ops._wrap(
                "local S = %s\nlocal h = rssHash(S)\n"
                "return {h = string.sub(h, 1, 8), n = #S}" % ops.lua_string_literal(sample)
            )
            got = client.execute_luau(code) or {}
            expected = py_fnv1a_32(sample.encode("utf-8"))
            ok = got.get("h") == expected and got.get("n") == len(sample.encode("utf-8"))
            matched += ok
            if not ok:
                print("      %-28r luau=%s python=%s" % (sample[:24], got.get("h"), expected))
        check("Luau rssHash == exact 32-bit FNV-1a on %d samples" % len(samples),
              matched == len(samples), "%d/%d matched" % (matched, len(samples)))

        print("\n-- 5. the hash uses its full 32 bits, not ~28 --")
        tails = set()
        for i in range(24):
            code = ops._wrap(
                "local h = rssHash(%s)\nreturn {t = string.sub(h, 7, 8)}"
                % ops.lua_string_literal("sample-%d" % i)
            )
            got = client.execute_luau(code) or {}
            tails.add(got.get("t"))
        high_nibbles = {t[0] for t in tails if t}
        check("final byte varies across samples (not always 0)",
              len(high_nibbles) > 4,
              "distinct high nibbles of final byte: %d" % len(high_nibbles))

        print("\n-- 6. hostile strings still round-trip byte-exactly --")
        hostile = [
            "]]", "]=]", "]==]", "[[", "[==[", "[[[", "%s%d%%", "{}", "\\", '"',
            "a\r\nb", "a\rb", "line\n", "\t", "\x01\x02\x03", "🎮" * 10,
            "@@TOKEN@@", "--[[ long ]]", 'back\\slash\\"quote',
        ]
        bad = [t[:20] for t in hostile if not roundtrip(t)]
        check("%d hostile strings round-trip exactly" % len(hostile), not bad, str(bad[:3]))

        print("\n-- 7. a real script body that ends in ']' survives a CAS write --")
        scratch = "ServerStorage.RSS_CodegenEdge"
        if ops.script_exists(client, scratch):
            ops.delete_script(client, scratch, ops.DELETE_CONFIRM_TOKEN)
        try:
            edge_body = "-- ends with a bracket, no trailing newline\nreturn t[1]"
            created = ops.create_script(client, "ServerStorage", "RSS_CodegenEdge",
                                        "ModuleScript", edge_body)
            check("create with a trailing ] succeeds", created.get("ok"), str(created.get("status")))
            fetched = ops.fetch_sources(client, [scratch]).get(scratch)
            check("content is byte-exact", fetched == edge_body, repr(fetched))

            new_body = "-- rewritten\nreturn a[b[1]]"
            res = ops.write_sources(client, [{
                "path": scratch, "source": new_body, "expected_fnv": created.get("fnv"),
            }]).get(scratch, {})
            check("CAS write of a trailing-]] body succeeds", res.get("status") == "written",
                  str(res.get("status")))
            check("rewritten content is byte-exact",
                  ops.fetch_sources(client, [scratch]).get(scratch) == new_body)
        finally:
            if ops.script_exists(client, scratch):
                ops.delete_script(client, scratch, ops.DELETE_CONFIRM_TOKEN)
            check("codegen scratch removed", not ops.script_exists(client, scratch))

    print("\n" + "=" * 74)
    print("PASSED %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print("  FAILED:", f)
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
