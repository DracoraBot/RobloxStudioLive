"""
Run every test suite and print one summary.

    python tests/run_all.py [--live] [--skip-offline]

Default runs the suites that need no coordination:
    test_sync_engine          offline decision logic
    selftest_studio_ops       bridge + codegen (needs Studio open)

`--live` additionally runs the suites that need a specific daemon state:
    acceptance_auto           requires `rss watch` RUNNING
    acceptance_safety         requires the daemon STOPPED
    acceptance_reconnect      manages its own connection

Those two cannot run at the same time, so they are not enabled by default.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

OFFLINE = [
    ("decision logic (no Studio needed)", "tests/test_sync_engine.py"),
]
STUDIO = [
    ("MCP bridge + Luau codegen", "tests/selftest_studio_ops.py"),
    ("codegen edge cases + exact FNV-1a", "tests/test_codegen_edges.py"),
    ("conflict copies stay bounded", "tests/test_conflict_bounded.py"),
]
LIVE = [
    ("reconnect after interruption", "tests/acceptance_reconnect.py"),
    ("safety: conflicts/deletions/renames", "tests/acceptance_safety.py"),
    ("live bidirectional sync (daemon must be running)", "tests/acceptance_auto.py"),
    ("soak: sustained operation + restart", "tests/acceptance_soak.py"),
]


def run(label: str, script: str) -> tuple[str, bool, float]:
    print("\n" + "=" * 74)
    print("RUNNING: %s  (%s)" % (label, script))
    print("=" * 74)
    started = time.time()
    proc = subprocess.run([PY, str(ROOT / script)], cwd=str(ROOT))
    return label, proc.returncode == 0, time.time() - started


def main(argv: list[str]) -> int:
    do_live = "--live" in argv
    skip_offline = "--skip-offline" in argv

    suites = ([] if skip_offline else OFFLINE) + STUDIO + (LIVE if do_live else [])
    results = [run(label, script) for label, script in suites]

    print("\n" + "=" * 74)
    print("SUMMARY")
    print("=" * 74)
    for label, ok, elapsed in results:
        print("  %-52s %-5s %5.1fs" % (label, "PASS" if ok else "FAIL", elapsed))
    failed = [label for label, ok, _ in results if not ok]
    print("\n  %d/%d suites passed" % (len(results) - len(failed), len(results)))
    if not do_live:
        print("\n  (run with --live to include the acceptance suites;")
        print("   start `rss watch` before acceptance_auto.py)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
