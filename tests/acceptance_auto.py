"""
Live acceptance test: does the running daemon move changes BY ITSELF?

Requires `rss watch` to already be running in another process.  This script only
observes -- it never touches sync-state.json -- and it restores every script it
edits, so the place ends exactly as it started.

    python tests/acceptance_auto.py

Covers MVP criteria:
    * editing a script in Studio updates the local file automatically
    * editing a local file updates the Studio script automatically
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import studio_ops  # noqa: E402
import sync_engine  # noqa: E402
from mcp_client import StudioMCPClient  # noqa: E402

TARGET = "ServerScriptService.TowerShared"
MARKER = "-- RSS-AUTO-TEST-MARKER"

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, (" -- " + detail) if detail else ""))


def set_studio_source(client, path: str, text: str) -> None:
    """Set Source directly, the way a human editing in Studio would."""
    code = (
        "local inst = game\n"
        "for _, part in ipairs(string.split(%s, '.')) do inst = inst:FindFirstChild(part) end\n"
        "inst.Source = %s\n"
        "return {ok = true, len = #inst.Source}\n"
    ) % (studio_ops.lua_quote(path), studio_ops.lua_string_literal(text))
    client.execute_luau(code)


def get_studio_source(client, path: str) -> str:
    return sync_engine.canonical(
        studio_ops.fetch_sources(client, [path]).get(path, "")
    )


def wait_for(predicate, timeout: float, interval: float = 0.25):
    """Poll until predicate() is truthy; return (ok, elapsed, last_value)."""
    started = time.time()
    last = None
    while time.time() - started < timeout:
        last = predicate()
        if last:
            return True, time.time() - started, last
        time.sleep(interval)
    return False, time.time() - started, last


def main() -> int:
    project = ROOT
    local_file = project / "src" / "ServerScriptService" / "TowerShared.luau"

    print("=" * 74)
    print("LIVE acceptance test (daemon must be running)")
    print("=" * 74)
    print("target : %s" % TARGET)
    print("local  : %s" % local_file.relative_to(project))

    if not local_file.is_file():
        print("local file missing -- run `rss import` first")
        return 2

    original = sync_engine.canonical(local_file.read_text(encoding="utf-8"))

    with StudioMCPClient() as client:
        studio_before = get_studio_source(client, TARGET)
        if studio_before != original:
            print("WARNING: studio and local already differ; aborting to avoid confusion")
            print("  studio sha %s" % sync_engine.sha256(studio_before)[:12])
            print("  local  sha %s" % sync_engine.sha256(original)[:12])
            return 2

        print("\n-- 1. STUDIO -> LOCAL (automatic) --")
        edited_by_studio = original + "\n" + MARKER + " studio-side edit\n"
        set_studio_source(client, TARGET, edited_by_studio)
        print("      wrote marker into Studio at %s" % time.strftime("%H:%M:%S"))

        ok, elapsed, _ = wait_for(
            lambda: sync_engine.canonical(local_file.read_text(encoding="utf-8")) == edited_by_studio,
            timeout=30,
        )
        check("studio edit reached the local file with no manual step", ok,
              "detected after %.1fs" % elapsed)
        check("local file matches the studio edit byte-for-byte",
              sync_engine.canonical(local_file.read_text(encoding="utf-8")) == edited_by_studio)

        print("\n-- 2. revert in Studio, local should follow --")
        set_studio_source(client, TARGET, original)
        ok, elapsed, _ = wait_for(
            lambda: sync_engine.canonical(local_file.read_text(encoding="utf-8")) == original,
            timeout=30,
        )
        check("studio-side revert propagated back", ok, "detected after %.1fs" % elapsed)

        print("\n-- 3. LOCAL -> STUDIO (automatic) --")
        edited_by_local = original + "\n" + MARKER + " local-side edit\n"
        local_file.write_text(edited_by_local, encoding="utf-8", newline="")
        print("      wrote marker into the local file at %s" % time.strftime("%H:%M:%S"))

        ok, elapsed, _ = wait_for(
            lambda: get_studio_source(client, TARGET) == edited_by_local,
            timeout=30,
        )
        check("local edit reached Studio with no manual step", ok,
              "detected after %.1fs" % elapsed)

        print("\n-- 4. revert locally, Studio should follow --")
        local_file.write_text(original, encoding="utf-8", newline="")
        ok, elapsed, _ = wait_for(
            lambda: get_studio_source(client, TARGET) == original,
            timeout=30,
        )
        check("local revert propagated back to Studio", ok, "detected after %.1fs" % elapsed)

        print("\n-- 5. no echo / no duplicate after settling --")
        time.sleep(5)
        snapshot = studio_ops.snapshot_scripts(client)
        versions = [p for p in snapshot if p.endswith(".TowerShared")]
        check("exactly one TowerShared in Studio (no duplicates)", len(versions) == 1, str(versions))
        check("final studio content equals the original",
              get_studio_source(client, TARGET) == original)
        check("final local content equals the original",
              sync_engine.canonical(local_file.read_text(encoding="utf-8")) == original)

    print("\n" + "=" * 74)
    print("PASSED %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print("  FAILED:", f)
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        import traceback

        traceback.print_exc()
        raise SystemExit(3)
