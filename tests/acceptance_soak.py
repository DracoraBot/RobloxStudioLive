"""
Sustained-operation ("soak") acceptance test.

The other live test proves a single edit flows each way.  This one proves the
daemon keeps working *autonomously over time*: many alternating edits, a daemon
restart mid-stream (the "Studio/daemon went away, did we lose anything?" case),
and a final convergence check.

Uses only a scratch script under ServerStorage, removed at the end.  The user's
own scripts are never touched.

    python tests/acceptance_soak.py [rounds]

Exit code 0 = all checks passed.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import studio_ops  # noqa: E402
import sync_engine  # noqa: E402
from mcp_client import StudioMCPClient  # noqa: E402

PY = sys.executable
SCRATCH = "ServerStorage.RSS_Soak"
SCRATCH_REL = "ServerStorage/RSS_Soak.luau"
PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, (" -- " + detail) if detail else ""))


def set_studio(client, path: str, text: str) -> None:
    code = (
        "local inst = game\n"
        "for _, part in ipairs(string.split(%s, '.')) do inst = inst:FindFirstChild(part) end\n"
        "if not inst then return {ok = false} end\n"
        "inst.Source = %s\n"
        "return {ok = true}\n"
    ) % (studio_ops.lua_quote(path), studio_ops.lua_string_literal(text))
    client.execute_luau(code)


def get_studio(client, path: str) -> str:
    return sync_engine.canonical(studio_ops.fetch_sources(client, [path]).get(path, ""))


def wait_for(predicate, timeout: float = 30.0):
    started = time.time()
    while time.time() - started < timeout:
        if predicate():
            return True, time.time() - started
        time.sleep(0.2)
    return False, time.time() - started


def start_daemon() -> subprocess.Popen:
    return subprocess.Popen(
        [PY, str(ROOT / "tools" / "rss.py"), "watch"],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def stop_daemon(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, text=True)
    try:
        proc.wait(timeout=15)
    except Exception:
        pass
    lock = ROOT / ".rss-daemon.lock"
    if lock.exists():
        lock.unlink()


def main(argv: list[str]) -> int:
    rounds = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 3
    print("=" * 74)
    print("SOAK acceptance test -- %d alternating rounds + restart" % rounds)
    print("=" * 74)

    target = ROOT / "src" / SCRATCH_REL
    daemon = None

    with StudioMCPClient() as client:
        baseline = studio_ops.snapshot_scripts(client)
        baseline_hashes = {p: m.get("fnv") for p, m in baseline.items()}

        # tidy any previous run
        if studio_ops.script_exists(client, SCRATCH):
            studio_ops.delete_script(client, SCRATCH, studio_ops.DELETE_CONFIRM_TOKEN)
        if target.is_file():
            target.unlink()

        try:
            print("\n-- 1. daemon picks up a Studio-side creation on its own --")
            body = "-- soak scratch, revision 0\nreturn { rev = 0 }\n"
            studio_ops.create_script(client, "ServerStorage", "RSS_Soak", "ModuleScript", body)

            daemon = start_daemon()
            ok, elapsed = wait_for(lambda: target.is_file() and
                                   sync_engine.canonical(target.read_text(encoding="utf-8")) == body,
                                   timeout=40)
            check("new Studio script appeared locally with no manual step", ok,
                  "after %.1fs" % elapsed)

            print("\n-- 2. %d alternating rounds (Studio edit <-> local edit) --" % rounds)
            latencies = []
            for rev in range(1, rounds * 2 + 1):
                if rev % 2 == 1:
                    text = body if rev == 1 else sync_engine.canonical(target.read_text(encoding="utf-8"))
                    text = text.rstrip("\n") + "\n-- studio edit rev %d\n" % rev
                    set_studio(client, SCRATCH, text)
                    ok, elapsed = wait_for(
                        lambda t=text: target.is_file()
                        and sync_engine.canonical(target.read_text(encoding="utf-8")) == t)
                    check("round %d studio -> local" % rev, ok, "%.1fs" % elapsed)
                else:
                    text = sync_engine.canonical(target.read_text(encoding="utf-8"))
                    text = text.rstrip("\n") + "\n-- local edit rev %d\n" % rev
                    target.write_text(text, encoding="utf-8", newline="")
                    ok, elapsed = wait_for(lambda t=text: get_studio(client, SCRATCH) == t)
                    check("round %d local -> studio" % rev, ok, "%.1fs" % elapsed)
                latencies.append(elapsed)

            check("every round propagated", len(latencies) == rounds * 2)
            print("      propagation latency: min %.1fs  max %.1fs  avg %.1fs" % (
                min(latencies), max(latencies), sum(latencies) / len(latencies)))

            print("\n-- 3. daemon restart resumes without losing anything --")
            stop_daemon(daemon)
            daemon = None
            offline_text = sync_engine.canonical(target.read_text(encoding="utf-8")) + \
                "\n-- edited locally while the daemon was DOWN\n"
            target.write_text(offline_text, encoding="utf-8", newline="")
            time.sleep(1.0)
            check("change did NOT reach Studio while the daemon was down",
                  get_studio(client, SCRATCH) != offline_text)

            daemon = start_daemon()
            ok, elapsed = wait_for(lambda: get_studio(client, SCRATCH) == offline_text, timeout=40)
            check("change made while down was synced after restart", ok,
                  "after %.1fs" % elapsed)

            print("\n-- 4. settle: no echo, no duplicates, converged --")
            time.sleep(6)
            snapshot = studio_ops.snapshot_scripts(client)
            dupes = [p for p in snapshot if p.endswith(".RSS_Soak")]
            check("exactly one scratch script in Studio (no duplicates)", len(dupes) == 1, str(dupes))
            check("both sides identical after settling",
                  get_studio(client, SCRATCH) == sync_engine.canonical(target.read_text(encoding="utf-8")))
            check("no spurious conflict was raised",
                  "conflict" not in str(sync_engine.SyncState.load(ROOT / "sync-state.json")
                                        .unresolved.get(SCRATCH, {})))

        finally:
            print("\n-- cleanup --")
            if daemon is not None:
                stop_daemon(daemon)
            try:
                if studio_ops.script_exists(client, SCRATCH):
                    studio_ops.delete_script(client, SCRATCH, studio_ops.DELETE_CONFIRM_TOKEN)
            except Exception:
                pass
            for path in [target, *target.parent.glob("RSS_Soak.conflict-*.luau")]:
                if path.is_file():
                    path.unlink()
            state = sync_engine.SyncState.load(ROOT / "sync-state.json")
            state.forget(SCRATCH)
            state.unresolved.pop(SCRATCH, None)
            state.save()
            subprocess.run([PY, str(ROOT / "tools" / "rss.py"), "sync"],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=300)

            final = studio_ops.snapshot_scripts(client)
            check("scratch removed from Studio", SCRATCH not in final)
            check("no RSS_ leftovers", not [p for p in final if "RSS_" in p])
            check("script count back to baseline", len(final) == len(baseline),
                  "%d vs %d" % (len(final), len(baseline)))
            changed = [p for p in final
                       if p in baseline_hashes and final[p].get("fnv") != baseline_hashes[p]]
            check("no original script content was modified", not changed, str(changed[:5]))

    print("\n" + "=" * 74)
    print("PASSED %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print("  FAILED:", f)
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
