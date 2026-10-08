"""
Acceptance test: reconnection after a Studio/MCP interruption.

Kills the bridge process underneath a live client and confirms:

  1. the raw MCP client transparently restarts and keeps working,
  2. a SyncDaemon cycle that was interrupted recovers on the next attempt,
  3. local files are untouched throughout.

This deliberately kills only the bridge process that *this test* spawned, so
the user's Studio session and the harness's own MCP connection are unaffected.

    python tests/acceptance_reconnect.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import rslog  # noqa: E402
import sync_engine  # noqa: E402
from daemon import SyncDaemon  # noqa: E402
from mcp_client import StudioMCPClient, StudioMCPError  # noqa: E402
from project import Project  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, (" -- " + detail) if detail else ""))


def kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True, text=True, timeout=30)


def main() -> int:
    print("=" * 74)
    print("RECONNECT acceptance test")
    print("=" * 74)

    with StudioMCPClient() as client:
        print("\n-- 1. baseline connection --")
        first = client.ping()
        check("connected before the interruption", first.get("connected"), str(first.get("error", ""))[:80])
        old_pid = client._proc.pid if client._proc else None
        check("bridge process known", bool(old_pid), "pid=%s" % old_pid)

        print("\n-- 2. kill the bridge process --")
        kill_tree(old_pid)
        time.sleep(1.0)
        check("client reports the process as dead", not client.alive)

        print("\n-- 3. next call must reconnect transparently --")
        try:
            second = client.ping()
            recovered = second.get("connected")
        except StudioMCPError as exc:
            second, recovered = {"error": str(exc)}, False
        check("client recovered without operator action", recovered, str(second)[:120])
        new_pid = client._proc.pid if client._proc else None
        check("a new bridge process was spawned", bool(new_pid) and new_pid != old_pid,
              "old=%s new=%s" % (old_pid, new_pid))
        check("studio still reachable after reconnect",
              bool(client.list_studios(refresh=True)))

        print("\n-- 4. a daemon cycle survives an interruption mid-run --")
        project = Project.discover(ROOT)
        logger = rslog.get_logger(project.logs_dir, level="INFO", name="reconnect-test")
        daemon = SyncDaemon(project, client, logger)

        before = sync_engine.local_index(project.src_dir)
        before_hashes = {k: v["sha256"] for k, v in before.items()}

        kill_tree(client._proc.pid)
        time.sleep(0.5)
        # Either outcome is acceptable and both are safe: the client may heal
        # itself inside the cycle, or the cycle may raise and the daemon's
        # backoff loop retries.  What matters is that recovery needs no
        # operator action and that nothing is lost.
        try:
            daemon.cycle()
            recovered_in_cycle = True
        except StudioMCPError:
            recovered_in_cycle = False
        check("interrupted cycle either self-healed or raised cleanly", True,
              "self-healed inside the cycle" if recovered_in_cycle
              else "raised, will retry on the next cycle")

        summary = daemon.cycle()
        check("next cycle succeeded after the interruption",
              not summary["errors"], str(summary["errors"])[:120])

        after = sync_engine.local_index(project.src_dir)
        after_hashes = {k: v["sha256"] for k, v in after.items()}
        check("local files survived the interruption intact",
              before_hashes == after_hashes,
              "%d files before, %d after" % (len(before_hashes), len(after_hashes)))
        check("no spurious sync churn after reconnecting",
              not summary["pulled"] and not summary["pushed"] and not summary["conflicts"],
              "pulled=%d pushed=%d conflicts=%d" % (
                  len(summary["pulled"]), len(summary["pushed"]), len(summary["conflicts"])))

    print("\n" + "=" * 74)
    print("PASSED %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print("  FAILED:", f)
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
