"""
Regression test for unbounded conflict-copy growth.

Bug found by an independent verifier: `_do_conflict` minted a fresh
timestamped `.conflict-<stamp>.luau` on *every* poll, because `state.mark()`
records status but never moves the content baseline, so the same conflict is
re-detected forever.  Left running, that is one new file every couple of
seconds -- tens of thousands overnight -- in a OneDrive-synced folder.

This test leaves a conflict deliberately unresolved and runs many cycles.  It
must produce exactly ONE conflict copy, no matter how many cycles elapse.

    python tests/test_conflict_bounded.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import rslog  # noqa: E402
import studio_ops  # noqa: E402
import sync_engine  # noqa: E402
from daemon import SyncDaemon  # noqa: E402
from mcp_client import StudioMCPClient  # noqa: E402
from project import Project  # noqa: E402

PY = sys.executable
SCRATCH = "ServerStorage.RSS_ConflictBounded"
SCRATCH_REL = "ServerStorage/RSS_ConflictBounded.luau"
CYCLES = 8

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, (" -- " + detail) if detail else ""))


def set_studio(client, path: str, text: str) -> None:
    code = (
        "local inst = game\n"
        "for _, part in ipairs(string.split(%s, '.')) do inst = inst:FindFirstChild(part) end\n"
        "if not inst then return {ok = false} end\n"
        "inst.Source = %s\nreturn {ok = true}\n"
    ) % (studio_ops.lua_quote(path), studio_ops.lua_string_literal(text))
    client.execute_luau(code)


def conflict_copies(target: Path) -> list[Path]:
    return sorted(target.parent.glob(target.stem + ".conflict-*.luau"))


def main() -> int:
    print("=" * 74)
    print("conflict-copy bounding regression test")
    print("=" * 74)

    project = Project.discover(ROOT)
    logger = rslog.get_logger(project.logs_dir, name="conflict-bounded")
    target = ROOT / "src" / SCRATCH_REL

    with StudioMCPClient() as client:
        if studio_ops.script_exists(client, SCRATCH):
            studio_ops.delete_script(client, SCRATCH, studio_ops.DELETE_CONFIRM_TOKEN)
        for stale in conflict_copies(target) + ([target] if target.is_file() else []):
            stale.unlink()

        try:
            original = "-- conflict bounding scratch\nreturn { v = 0 }\n"
            studio_ops.create_script(client, "ServerStorage", "RSS_ConflictBounded",
                                     "ModuleScript", original)
            target.write_text(original, encoding="utf-8", newline="")

            # baseline it so both sides have a common ancestor
            subprocess.run([PY, str(ROOT / "tools" / "rss.py"), "sync"],
                           cwd=str(ROOT), capture_output=True, timeout=300)

            # now diverge both sides, leaving the conflict unresolved
            studio_version = "-- conflict bounding scratch\nreturn { v = 'STUDIO' }\n"
            local_version = "-- conflict bounding scratch\nreturn { v = 'LOCAL' }\n"
            set_studio(client, SCRATCH, studio_version)
            target.write_text(local_version, encoding="utf-8", newline="")

            daemon = SyncDaemon(project, client, logger)
            counts = []
            for i in range(CYCLES):
                summary = daemon.cycle()
                counts.append(len(conflict_copies(target)))
                if i == 0:
                    check("first cycle parked the conflict", counts[0] == 1,
                          "%d file(s)" % counts[0])

            print("      conflict copies after each of %d cycles: %s" % (CYCLES, counts))
            check("exactly one conflict copy after %d cycles" % CYCLES,
                  counts[-1] == 1, "%d file(s)" % counts[-1])
            check("copy count never grew across cycles",
                  all(c <= 1 for c in counts), str(counts))
            check("later cycles were recognised as repeats",
                  all(c == 1 for c in counts[1:]) if len(counts) > 1 else True,
                  str(counts))

            state = sync_engine.SyncState.load(project.state_path)
            sig = state.get(SCRATCH).get("conflictSignature")
            check("conflict signature recorded on the entry", bool(sig), str(sig))

            # local and studio content must both still be intact
            check("local content untouched",
                  sync_engine.canonical(target.read_text(encoding="utf-8")) == local_version)
            fetched = studio_ops.fetch_sources(client, [SCRATCH])
            check("studio content untouched",
                  sync_engine.canonical(fetched.get(SCRATCH, "")) == studio_version)
            saved = conflict_copies(target)[0].read_text(encoding="utf-8")
            check("parked copy holds the Studio version", studio_version in saved)

            # --- a NEW studio edit must supersede the old copy, not add a 2nd ---
            print("\n-- a fresh Studio edit should refresh, not accumulate --")
            studio_version2 = "-- conflict bounding scratch\nreturn { v = 'STUDIO-2' }\n"
            set_studio(client, SCRATCH, studio_version2)
            daemon.cycle()
            after = conflict_copies(target)
            check("still exactly one conflict copy after the Studio moved on",
                  len(after) == 1, "%d file(s): %s" % (len(after), [p.name for p in after]))
            if after:
                check("the single copy now holds the newer Studio version",
                      studio_version2 in after[0].read_text(encoding="utf-8"))

        finally:
            print("\n-- cleanup --")
            try:
                if studio_ops.script_exists(client, SCRATCH):
                    studio_ops.delete_script(client, SCRATCH, studio_ops.DELETE_CONFIRM_TOKEN)
            except Exception:
                pass
            for path in conflict_copies(target) + ([target] if target.is_file() else []):
                path.unlink()
            state = sync_engine.SyncState.load(project.state_path)
            state.forget(SCRATCH)
            state.save()
            subprocess.run([PY, str(ROOT / "tools" / "rss.py"), "sync"],
                           cwd=str(ROOT), capture_output=True, timeout=300)
            check("scratch removed from Studio", not studio_ops.script_exists(client, SCRATCH))
            check("no conflict copies left behind", not conflict_copies(target))

    print("\n" + "=" * 74)
    print("PASSED %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print("  FAILED:", f)
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
