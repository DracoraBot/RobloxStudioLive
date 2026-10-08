"""
RobloxStudioLive - first-run setup.

    python tools/init.py [options]

`init.cmd` runs this after it has located a usable Python interpreter, but this
is an ordinary program: run it directly if you prefer, or from CI with --check.

Why it exists
-------------
The daemon needs several files that must never be committed to a public
repository, because each one carries a trace of the author's own game:

    config/settings.json     which services to mirror, and your placeId
    config/python-path.txt   the interpreter validated on this machine
    src/                     the scripts mirrored out of your place
    backups/                 full snapshots of both sides
    logs/                    an audit trail with absolute paths in every line
    sync-state.json          the 3-way diff baseline for your place

All of them are covered by `.gitignore`.  A fresh clone therefore has none of
them, and this program is what creates them - from the template that *is*
committed, never from anyone else's machine.

Every step is idempotent.  Running it twice changes nothing the second time,
and nothing that already exists is overwritten unless you pass --force.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
TESTS = ROOT / "tests"

EXAMPLE_SETTINGS = ROOT / "config" / "settings.example.json"
SETTINGS = ROOT / "config" / "settings.json"
ROJO_PROJECT = ROOT / "default.project.json"
PYTHON_RECORD = ROOT / "config" / "python-path.txt"

FALLBACK_ROOTS = [
    "ServerScriptService", "ReplicatedStorage", "StarterGui", "StarterPlayer",
    "ReplicatedFirst", "ServerStorage", "Workspace",
]

# Importing a module is a stronger check than compiling it: it runs the module
# body and resolves every import it makes.  These are all leaves or near-leaves
# with no import-time side effects, so a failure here means a genuinely broken
# checkout rather than a surprise.
TOOL_MODULES = ("project", "rslog", "sync_engine", "backup", "mcp_client",
                "studio_ops", "daemon", "rss")

MARKS = {"ok": "[ ok ]", "warn": "[warn]", "fail": "[fail]", "skip": "[skip]"}


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
class Report:
    """Collects one row per thing that was checked, then prints or serialises."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, level: str, title: str, detail: str = "") -> None:
        self.rows.append({"level": level, "title": title, "detail": detail})

    @property
    def failed(self) -> bool:
        return any(row["level"] == "fail" for row in self.rows)

    @property
    def warnings(self) -> int:
        return sum(1 for row in self.rows if row["level"] == "warn")

    def print_human(self) -> None:
        width = max((len(row["title"]) for row in self.rows), default=8)

        sys.stdout.write("\n  RobloxStudioLive - setup\n")
        sys.stdout.write("  =======================\n\n")
        sys.stdout.write("  python   : %s   (%s)\n" % (
            sys.version.split()[0], os.environ.get("RSS_PY_SOURCE") or "direct"))
        sys.stdout.write("  project  : %s\n\n" % ROOT)

        for row in self.rows:
            lines = str(row["detail"]).splitlines() or [""]
            sys.stdout.write("  %s %-*s  %s\n" % (
                MARKS[row["level"]], width, row["title"], lines[0]))
            for extra in lines[1:]:
                sys.stdout.write("  %s %-*s  %s\n" % (" " * 6, width, "", extra))

        sys.stdout.write("\n")
        if self.failed:
            sys.stdout.write("  Setup finished with problems. See the [fail] rows above.\n")
        elif self.warnings:
            sys.stdout.write("  Setup complete (%d warning%s).\n" % (
                self.warnings, "" if self.warnings == 1 else "s"))
        else:
            sys.stdout.write("  Setup complete.\n")
        sys.stdout.write("\n")


def _next_steps() -> str:
    return (
        "  Next\n"
        "  ----\n"
        "  1. Open Roblox Studio and load the place you want to mirror.  The\n"
        "     daemon drives Studio through Studio's own MCP server, so Studio\n"
        "     has to be open in Edit mode.  Nothing is installed into your place\n"
        "     and no project setting is changed.\n"
        "\n"
        "  2. Pull your existing scripts down first - this reads Studio and\n"
        "     writes nothing to it:\n"
        "\n"
        "         python tools\\rss.py import\n"
        "\n"
        "  3. Start the live daemon:\n"
        "\n"
        "         start-sync.cmd\n"
        "\n"
        "  4. Check on it from another window:\n"
        "\n"
        "         python tools\\rss.py status\n"
        "         python tools\\rss.py conflicts\n"
        "\n"
        "  Everything private - your scripts, snapshots, logs, settings and\n"
        "  the interpreter path - is already in .gitignore, so `git status`\n"
        "  stays clean of it.  Verify with:  git status --short\n"
    )


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, payload) -> None:
    """Atomic write, same discipline as the daemon: never a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".rss-tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="")
    os.replace(tmp, path)


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("/", "\\")
    except ValueError:
        return str(path)


def _parse_roots(value: str | None) -> list[str] | None:
    if not value:
        return None
    roots = [part.strip() for part in value.split(",") if part.strip()]
    return roots or None


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------
def step_folders(dry_run: bool, report: Report) -> None:
    wanted = ("src", "backups", "logs", "config", "docs")
    missing = [name for name in wanted if not (ROOT / name).is_dir()]
    if not missing:
        report.add("ok", "folders", "%s already exist" % ", ".join(wanted))
        return
    if dry_run:
        report.add("ok", "folders", "would create %s" % ", ".join(missing))
        return
    for name in missing:
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    report.add("ok", "folders", "created %s" % ", ".join(missing))


def _settings_template(roots: list[str] | None) -> dict:
    payload = _read_json(EXAMPLE_SETTINGS)
    if payload is None:
        try:
            sys.path.insert(0, str(TOOLS))
            import project  # noqa: PLC0415 - only needed on this fallback path
            payload = json.loads(json.dumps(project.DEFAULT_SETTINGS))
        except Exception:
            payload = {"version": 1, "project": "RobloxStudioLive",
                       "roots": list(FALLBACK_ROOTS)}
    payload = json.loads(json.dumps(payload))  # deep copy
    payload.setdefault("roots", list(FALLBACK_ROOTS))
    if roots:
        payload["roots"] = list(roots)
    return payload


def step_settings(roots: list[str] | None, force: bool, dry_run: bool,
                  report: Report) -> dict:
    """Create config/settings.json; return the settings that are now in force."""
    template = _settings_template(roots)
    existing = _read_json(SETTINGS)

    if existing is None:
        if dry_run:
            report.add("ok", "settings", "would create %s" % _display(SETTINGS))
        else:
            _write_json(SETTINGS, template)
            report.add("ok", "settings", "created %s from the template" % _display(SETTINGS))
        return template

    if force:
        backup = SETTINGS.with_name(SETTINGS.name + ".bak")
        if dry_run:
            report.add("ok", "settings", "would rebuild %s (old copy -> %s)" % (
                _display(SETTINGS), _display(backup)))
        else:
            _write_json(backup, existing)
            _write_json(SETTINGS, template)
            report.add("ok", "settings", "rebuilt %s; previous version kept as %s" % (
                _display(SETTINGS), _display(backup)))
        return template

    absent = [key for key in ("roots", "sync", "mcp") if key not in existing]
    if absent:
        report.add("warn", "settings",
                   "%s has no %s - built-in defaults will be used for those"
                   % (_display(SETTINGS), ", ".join(absent)))
    else:
        count = len(existing.get("roots") or [])
        report.add("ok", "settings", "kept %s (%d service%s mirrored)" % (
            _display(SETTINGS), count, "" if count == 1 else "s"))
    return existing


def _rojo_project(roots: list[str]) -> dict:
    tree: dict = {"$className": "DataModel"}
    for service in roots:
        if service == "StarterPlayer":
            # The only service here whose scripts live one level deeper than
            # the service itself, so it is the only one that needs children.
            tree[service] = {
                "$className": "StarterPlayer",
                "StarterPlayerScripts": {"$path": "src/StarterPlayer/StarterPlayerScripts"},
                "StarterCharacterScripts": {"$path": "src/StarterPlayer/StarterCharacterScripts"},
            }
        else:
            tree[service] = {"$path": "src/%s" % service}
    return {"name": "RobloxStudioLive", "tree": tree}


def step_rojo(roots: list[str], force: bool, dry_run: bool, report: Report) -> None:
    """Keep the optional Rojo file in step with the configured roots.

    Rojo is not required - the daemon does its own path mapping - so a mismatch
    is a warning, never a failure.
    """
    wanted = _rojo_project(roots)
    existing = _read_json(ROJO_PROJECT)

    if existing is None:
        if dry_run:
            report.add("ok", "rojo project", "would create %s" % _display(ROJO_PROJECT))
        else:
            _write_json(ROJO_PROJECT, wanted)
            report.add("ok", "rojo project",
                       "created %s for %d services" % (_display(ROJO_PROJECT), len(roots)))
        return

    if existing == wanted:
        report.add("ok", "rojo project", "%s already matches your roots" % _display(ROJO_PROJECT))
        return

    if force:
        if dry_run:
            report.add("ok", "rojo project", "would rebuild %s" % _display(ROJO_PROJECT))
        else:
            _write_json(ROJO_PROJECT, wanted)
            report.add("ok", "rojo project", "rebuilt %s" % _display(ROJO_PROJECT))
        return

    tree = existing.get("tree")
    have = set(tree) - {"$className"} if isinstance(tree, dict) else set()
    extra = sorted(have - set(roots))
    absent = sorted(set(roots) - have)
    difference = []
    if absent:
        difference.append("missing " + ", ".join(absent))
    if extra:
        difference.append("unexpected " + ", ".join(extra))
    report.add("warn", "rojo project",
               "%s does not match your roots (%s); re-run with --force to "
               "regenerate it, or ignore this if you do not use Rojo"
               % (_display(ROJO_PROJECT), "; ".join(difference) or "different shape"))


def step_interpreter(dry_run: bool, report: Report) -> None:
    """Record the interpreter that init just proved works.

    Written because the launchers have to guess otherwise, and on Windows the
    first `python.exe` on PATH is frequently the Microsoft Store stub.
    """
    exe = sys.executable
    if not exe:
        report.add("warn", "python path",
                   "this interpreter does not report its own path; the "
                   "launchers will search PATH each time")
        return
    value = '"%s"' % exe if " " in exe else exe
    if dry_run:
        report.add("ok", "python path", "would record %s" % value)
        return
    PYTHON_RECORD.parent.mkdir(parents=True, exist_ok=True)
    PYTHON_RECORD.write_text(value + "\n", encoding="utf-8", newline="")
    report.add("ok", "python path", "%s -> %s" % (_display(PYTHON_RECORD), value))


def step_checkout(report: Report) -> None:
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    broken = []
    for name in TOOL_MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - the message is the point
            broken.append("%s (%s)" % (name, exc))
    if broken:
        report.add("fail", "checkout", "these modules do not import: %s" % "; ".join(broken))
    else:
        report.add("ok", "checkout", "%d tool modules import cleanly" % len(TOOL_MODULES))


def step_studio(report: Report) -> None:
    """Report whether Roblox's own MCP binary is installed.

    A warning, not a failure: Studio can be installed or updated after setup,
    and nothing else in this project depends on it being present right now.
    """
    try:
        if str(TOOLS) not in sys.path:
            sys.path.insert(0, str(TOOLS))
        import mcp_client
        exe = mcp_client.find_studio_mcp_exe()
    except Exception as exc:  # noqa: BLE001
        report.add("warn", "studio binary",
                   "%s Install or update Roblox Studio, then re-run init.cmd." % str(exc)[:220])
        return
    report.add("ok", "studio binary", exe)


def step_selftest(dry_run: bool, report: Report) -> None:
    """Prove the checkout works, without going anywhere near Studio."""
    script = TESTS / "test_sync_engine.py"
    if not script.is_file():
        report.add("warn", "self-test", "%s is missing; skipped" % _display(script))
        return
    if dry_run:
        report.add("skip", "self-test", "would run %s" % _display(script))
        return
    try:
        proc = subprocess.run(
            [sys.executable, str(script)], cwd=str(ROOT),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        report.add("warn", "self-test", "could not run %s: %s" % (_display(script), exc))
        return

    output = "%s\n%s" % (proc.stdout or "", proc.stderr or "")
    match = re.search(r"(PASSED|FAILED)\s+(\d+)\s*/\s*(\d+)", output)
    if proc.returncode == 0 and match:
        report.add("ok", "self-test", "%s: %s/%s assertions pass" % (
            _display(script), match.group(2), match.group(3)))
        return
    tail = [line for line in output.strip().splitlines() if line.strip()][-6:]
    report.add("fail", "self-test", "%s failed (exit %d):\n%s" % (
        _display(script), proc.returncode, "\n".join(tail)))


def step_studio_link(report: Report) -> None:
    """--studio: ask a running Studio whether the bridge actually connects."""
    script = TOOLS / "rss.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "status", "--json"], cwd=str(ROOT),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        report.add("warn", "studio link", "could not run %s: %s" % (_display(script), exc))
        return

    try:
        status = json.loads(proc.stdout or "{}")
    except ValueError:
        status = {}

    studios = status.get("studios") or []
    if status.get("connected") and studios:
        names = ", ".join(str(s.get("name")) for s in studios)
        place = ((status.get("place") or {}).get("name")) or "unnamed place"
        report.add("ok", "studio link", "connected to %s (%s), %s scripts visible" % (
            names, place, status.get("studioScripts")))
        return

    detail = status.get("error") or "no Studio instance is attached"
    report.add("warn", "studio link",
               "%s. Open Studio with your place in Edit mode and re-run "
               "`python tools\\rss.py status`." % str(detail)[:220])


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="init.py",
        description="RobloxStudioLive - first-run setup.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples\n"
            "  python tools/init.py                  set up this checkout\n"
            "  python tools/init.py --check          show what would change\n"
            "  python tools/init.py --studio         also test the Studio link\n"
            "  python tools/init.py --force --roots ServerScriptService,ReplicatedStorage\n"
            "                                        mirror only two services\n"
        ),
    )
    parser.add_argument("--check", action="store_true",
                        help="report what would happen and change nothing")
    parser.add_argument("--force", action="store_true",
                        help="rebuild config/settings.json and default.project.json "
                             "(the previous settings are kept as settings.json.bak)")
    parser.add_argument("--roots", metavar="A,B,C",
                        help="services to mirror; used when creating settings.json, "
                             "and with --force to change an existing one")
    parser.add_argument("--no-tests", action="store_true",
                        help="skip the offline self-test")
    parser.add_argument("--studio", action="store_true",
                        help="also check the connection to a running Roblox Studio")
    parser.add_argument("--json", action="store_true",
                        help="print a machine-readable result instead of the report")
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv[1:])
    report = Report()

    step_folders(args.check, report)
    settings = step_settings(_parse_roots(args.roots), args.force, args.check, report)
    roots = [str(name) for name in (settings.get("roots") or FALLBACK_ROOTS)]
    step_rojo(roots, args.force, args.check, report)
    step_interpreter(args.check, report)
    step_checkout(report)
    step_studio(report)
    if not args.no_tests:
        step_selftest(args.check, report)
    if args.studio:
        step_studio_link(report)

    if args.json:
        print(json.dumps({
            "ok": not report.failed,
            "check": args.check,
            "project": str(ROOT),
            "steps": report.rows,
        }, indent=2))
        return 1 if report.failed else 0

    report.print_human()
    if not report.failed:
        print(_next_steps())
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
