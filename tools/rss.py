"""
RobloxStudioLive - command line interface.

    python tools/rss.py <command> [options]

Commands
--------
    status                 connection, counts, unresolved items
    import                 first-time Studio -> local import (never writes Studio)
    sync                   run exactly one sync cycle and exit
    watch                  run the live daemon (default command)
    conflicts              list items needing a decision
    resolve --path P --take studio|local
    diff --path P          show the two versions of one script
    tree                   dump the Studio instance tree to config/structure.json
    verify                 confirm Studio and disk agree for every tracked script
    backup [label]         timestamped backup of both sides

Run any command with --help for its options.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import backup as backup_mod  # noqa: E402
import rslog  # noqa: E402
import studio_ops  # noqa: E402
import sync_engine  # noqa: E402
from daemon import SyncDaemon  # noqa: E402
from mcp_client import StudioMCPClient, StudioMCPError, broker_health, find_studio_mcp_exe  # noqa: E402
from project import Project  # noqa: E402


# --------------------------------------------------------------------------
def make_client(project: Project) -> StudioMCPClient:
    settings = project.settings.get("mcp") or {}
    return StudioMCPClient(
        exe_path=settings.get("exePath"),
        datamodel=settings.get("datamodel", "Edit"),
        timeout=float(settings.get("requestTimeoutSeconds", 120)),
    )


def get_logger(project: Project, args) -> object:
    level = "DEBUG" if getattr(args, "verbose", False) else \
        (project.settings.get("logging") or {}).get("level", "INFO")
    return rslog.get_logger(project.logs_dir, level=level)


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_status(args) -> int:
    project = Project.discover()
    health = broker_health()
    out = {
        "project": str(project.root),
        "studioMcpExe": find_studio_mcp_exe(),
        "broker": health,
    }
    try:
        with make_client(project) as client:
            studios = client.list_studios(refresh=True)
            out["studios"] = studios
            out["connected"] = bool(studios)
            if studios:
                info = client.execute_luau(
                    "return {name = game.Name, placeId = game.PlaceId, edits = 0}"
                )
                out["place"] = info
                snapshot = studio_ops.snapshot_scripts(client, project.roots)
                out["studioScripts"] = len(snapshot)
                out["studioBytes"] = sum(int(m.get("bytes") or 0) for m in snapshot.values())
    except StudioMCPError as exc:
        out["connected"] = False
        out["error"] = str(exc)[:300]

    state = sync_engine.SyncState.load(project.state_path)
    local = sync_engine.local_index(project.src_dir, project.sync_settings.get("ignoreLocalGlobs"))
    out["localScripts"] = len(local)
    out["trackedScripts"] = len(state.entries)
    out["unresolved"] = state.unresolved
    out["backups"] = len([d for d in project.backups_dir.iterdir() if d.is_dir()]) \
        if project.backups_dir.is_dir() else 0

    if args.json:
        _print(out)
    else:
        print("RobloxStudioLive status")
        print("  project        : %s" % out["project"])
        print("  StudioMCP.exe  : %s" % out["studioMcpExe"])
        print("  broker         : %s" % (health or "unreachable"))
        print("  connected      : %s" % out.get("connected"))
        for studio in (out.get("studios") or []):
            print("  studio         : %s  (%s)" % (studio.get("name"), studio.get("id")))
        if out.get("place"):
            print("  place          : %s (placeId %s)" % (out["place"].get("name"), out["place"].get("placeId")))
        print("  studio scripts : %s (%s bytes)" % (out.get("studioScripts"), out.get("studioBytes")))
        print("  local scripts  : %s" % out["localScripts"])
        print("  tracked        : %s" % out["trackedScripts"])
        print("  backups        : %s" % out["backups"])
        if state.unresolved:
            print("  unresolved     : %d" % len(state.unresolved))
            for path, item in sorted(state.unresolved.items()):
                print("     - %-58s %s" % (path, item.get("status")))
        else:
            print("  unresolved     : none")
    return 0


def cmd_import(args) -> int:
    """First import: pull everything from Studio, never write to Studio."""
    project = Project.discover()
    logger = get_logger(project, args)
    with make_client(project) as client:
        daemon = SyncDaemon(project, client, logger, dry_run=args.dry_run, pull_only=True)
        summary = daemon.cycle()
    logger.info("import complete: %s", json.dumps({
        k: (len(v) if isinstance(v, list) else v) for k, v in summary.items()
    }))
    if args.json:
        _print(summary)
    else:
        print("Imported %d scripts from Studio into %s" % (len(summary["pulled"]), project.src_dir))
        for path in summary["pulled"]:
            print("   +", path)
        if summary["errors"]:
            print("Errors:")
            for err in summary["errors"]:
                print("   !", err)
    return 1 if summary["errors"] else 0


def cmd_sync(args) -> int:
    project = Project.discover()
    logger = get_logger(project, args)
    with exclusive(project) as lock:
        if not lock.acquired:
            print("another RobloxStudioLive process is already syncing this project "
                  "(pid %s). Read-only commands (status/conflicts/verify) still work." % lock.holder)
            return 2
        with make_client(project) as client:
            daemon = SyncDaemon(project, client, logger, dry_run=args.dry_run,
                                pull_only=args.pull_only)
            summary = daemon.cycle()
    if args.json:
        _print(summary)
    else:
        print("cycle: pulled=%d pushed=%d created=%d renamed=%d conflicts=%d "
              "missingStudio=%d missingLocal=%d errors=%d (%dms)" % (
                  len(summary["pulled"]), len(summary["pushed"]), len(summary["created"]),
                  len(summary["renamed"]), len(summary["conflicts"]),
                  len(summary["missingStudio"]), len(summary["missingLocal"]),
                  len(summary["errors"]), summary["durationMs"]))
        for key in ("pulled", "pushed", "created", "conflicts", "missingStudio", "missingLocal"):
            for item in summary[key]:
                print("   %-14s %s" % (key, item))
        for err in summary["errors"]:
            print("   ERROR          %s" % err)
    return 1 if summary["errors"] else 0


# --------------------------------------------------------------------------
# exclusive access
# --------------------------------------------------------------------------
class _Lock:
    """Result of `exclusive()`; `acquired` says whether we own the lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False
        self.holder: int | str = "?"

    def release(self) -> None:
        if self.acquired:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.acquired = False


class exclusive:
    """Serialise anything that mutates Studio or src/.

    `rss sync` and `rss watch` both write the same tree and the same Studio
    place.  Without this, two of them racing produce exactly the kind of
    interleaving that looks like churn: one process' scratch script showing up
    as another's "missing-in-studio".  Read-only commands take no lock.
    """

    def __init__(self, project: Project) -> None:
        self._lock = _Lock(project.lock_path)

    def __enter__(self) -> _Lock:
        lock = self._lock
        if lock.path.exists():
            try:
                pid = int(lock.path.read_text(encoding="utf-8").strip().splitlines()[0])
            except (OSError, ValueError, IndexError):
                pid = -1
            if pid > 0 and pid != os.getpid() and _pid_alive(pid):
                lock.holder = pid
                return lock
            try:
                lock.path.unlink()          # stale lock from a dead process
            except OSError:
                pass
        lock.path.parent.mkdir(parents=True, exist_ok=True)
        lock.path.write_text("%d\n%s\n" % (os.getpid(), time.strftime("%Y-%m-%dT%H:%M:%S")),
                             encoding="utf-8")
        lock.acquired = True
        return lock

    def __exit__(self, *exc: object) -> None:
        self._lock.release()


def cmd_watch(args) -> int:
    project = Project.discover()
    logger = get_logger(project, args)
    with exclusive(project) as lock:
        if not lock.acquired:
            print("a daemon is already running (pid %s). Stop it first." % lock.holder)
            return 2
        try:
            with make_client(project) as client:
                daemon = SyncDaemon(project, client, logger, dry_run=args.dry_run,
                                    pull_only=args.pull_only)
                if args.cycles:
                    for _ in range(args.cycles):
                        daemon.cycle()
                    return 0
                daemon.run()
        except KeyboardInterrupt:
            logger.info("interrupted")
    return 0


def cmd_conflicts(args) -> int:
    project = Project.discover()
    state = sync_engine.SyncState.load(project.state_path)
    items = []
    for path, item in sorted(state.unresolved.items()):
        entry = state.get(path)
        items.append({
            "studioPath": path,
            "rel": entry.get("rel"),
            "status": item.get("status"),
            "detail": item.get("detail"),
            "at": item.get("at"),
        })
    conflict_files = sorted(
        str(p.relative_to(project.root)).replace("\\", "/")
        for p in project.src_dir.rglob("*.conflict-*.luau")
    )
    if args.json:
        _print({"unresolved": items, "conflictFiles": conflict_files})
        return 0
    if not items and not conflict_files:
        print("No conflicts. Everything tracked is in sync.")
        return 0
    print("%d unresolved item(s):" % len(items))
    for item in items:
        print("  %-16s %-52s %s" % (item["status"], item["studioPath"], item["detail"] or ""))
    if conflict_files:
        print("\nConflict copies on disk (%d):" % len(conflict_files))
        for name in conflict_files:
            print("  ", name)
    print("\nResolve with: python tools/rss.py resolve --path \"<StudioPath>\" --take studio|local")
    return 0


def cmd_resolve(args) -> int:
    project = Project.discover()
    logger = get_logger(project, args)
    state = sync_engine.SyncState.load(project.state_path)
    entry = state.get(args.path)
    if not entry:
        print("unknown path: %s" % args.path)
        return 2
    rel = entry.get("rel")
    target = project.src_dir / rel

    with make_client(project) as client:
        snapshot = studio_ops.snapshot_scripts(client, project.roots)
        meta = snapshot.get(args.path) or {}
        fetched = studio_ops.fetch_sources(
            client, [args.path],
            sizes={args.path: int(meta.get("bytes") or 0)},
        ) if meta else {}
        studio_text = sync_engine.canonical(fetched.get(args.path, ""))
        local_text = sync_engine.canonical(target.read_text(encoding="utf-8")) if target.is_file() else ""

        if args.take == "studio":
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".rss-tmp")
            tmp.write_text(studio_text, encoding="utf-8", newline="")
            os.replace(tmp, target)
            state.record(args.path, class_name=entry.get("className", "ModuleScript"),
                         rel=rel, content_hash=sync_engine.sha256(studio_text),
                         studio_fnv=meta.get("fnv"), direction="resolve-studio")
            logger.info("resolved %s -> took the STUDIO version", args.path)
        else:
            if not meta:
                print("cannot take the local version: %s does not exist in Studio." % args.path)
                print("Run `rss sync` to push it to Studio as a new script instead.")
                return 2
            result = studio_ops.write_sources(client, [{
                "path": args.path, "source": local_text, "expected_fnv": meta.get("fnv"),
            }]).get(args.path, {})
            if result.get("status") != "written":
                print("could not write to Studio: %s" % result.get("status"))
                return 1
            state.record(args.path, class_name=entry.get("className", "ModuleScript"),
                         rel=rel, content_hash=sync_engine.sha256(local_text),
                         studio_fnv=result.get("newFnv"), direction="resolve-local")
            logger.info("resolved %s -> took the LOCAL version", args.path)

    state.save()
    # Remove only THIS script's parked copy.  A previous version filtered a
    # recursive glob with `name.startswith(Path(rel).stem)`, so resolving
    # "Foo" also deleted "FooBar.conflict-*.luau" and "FooTwo.conflict-*.luau"
    # -- unrelated items whose parked Studio version is the only local record
    # of that content.  Scoping the glob to this exact stem cannot over-match.
    for conflict in sorted(target.parent.glob(target.stem + ".conflict-*.luau")):
        try:
            conflict.unlink()
            logger.info("removed resolved conflict copy %s", conflict.name)
        except OSError:
            pass
    print("resolved %s (took %s)" % (args.path, args.take))
    return 0


def cmd_diff(args) -> int:
    project = Project.discover()
    state = sync_engine.SyncState.load(project.state_path)
    entry = state.get(args.path)
    rel = (entry or {}).get("rel") or sync_engine.rel_path_for(
        args.path, (entry or {}).get("className", "ModuleScript"))
    target = project.src_dir / rel

    with make_client(project) as client:
        snapshot = studio_ops.snapshot_scripts(client, project.roots)
        meta = snapshot.get(args.path) or {}
        fetched = studio_ops.fetch_sources(
            client, [args.path], sizes={args.path: int(meta.get("bytes") or 0)}
        ) if meta else {}
        studio_text = sync_engine.canonical(fetched.get(args.path, ""))
    local_text = sync_engine.canonical(target.read_text(encoding="utf-8")) if target.is_file() else ""

    import difflib
    diff = list(difflib.unified_diff(
        local_text.splitlines(), studio_text.splitlines(),
        fromfile="local:%s" % rel, tofile="studio:%s" % args.path, lineterm="",
    ))
    print("\n".join(diff) if diff else "identical (%s)" % sync_engine.sha256(studio_text)[:12])
    return 0


def cmd_tree(args) -> int:
    project = Project.discover()
    with make_client(project) as client:
        tree = studio_ops.instance_tree(client, args.root, max_depth=args.depth)
        snapshot = studio_ops.snapshot_scripts(client, project.roots)
    payload = {
        "capturedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "root": args.root,
        "nodeCount": len(tree),
        "scripts": {p: {"className": m.get("className"), "bytes": m.get("bytes")}
                    for p, m in sorted(snapshot.items())},
        "nodes": tree,
    }
    project.structure_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print("wrote %d nodes and %d scripts to %s" % (len(tree), len(snapshot), project.structure_path))
    if not args.json:
        services = {}
        for path, meta in snapshot.items():
            services.setdefault(path.split(".")[0], 0)
            services[path.split(".")[0]] += 1
        for service, count in sorted(services.items()):
            print("  %-24s %d script(s)" % (service, count))
    return 0


def cmd_verify(args) -> int:
    """Confirm every tracked script is byte-identical on both sides."""
    project = Project.discover()
    state = sync_engine.SyncState.load(project.state_path)
    local = sync_engine.local_index(project.src_dir, project.sync_settings.get("ignoreLocalGlobs"))

    with make_client(project) as client:
        snapshot = studio_ops.snapshot_scripts(client, project.roots)
        sizes = {p: int(m.get("bytes") or 0) for p, m in snapshot.items()}
        sources = studio_ops.fetch_sources(client, list(snapshot), sizes=sizes)

    mismatches, missing_local, ok = [], [], 0
    for path, meta in sorted(snapshot.items()):
        rel = sync_engine.rel_path_for(path, meta.get("className", "ModuleScript"))
        studio_sha = sync_engine.sha256(sync_engine.canonical(sources.get(path, "")))
        local_entry = local.get(rel)
        if not local_entry:
            missing_local.append({"studioPath": path, "rel": rel})
            continue
        if local_entry["sha256"] != studio_sha:
            mismatches.append({"studioPath": path, "rel": rel,
                               "studioSha": studio_sha, "localSha": local_entry["sha256"]})
        else:
            ok += 1

    result = {
        "inSync": ok, "mismatched": mismatches,
        "missingLocally": missing_local,
        "studioScripts": len(snapshot), "localScripts": len(local),
    }
    if args.json:
        _print(result)
    else:
        print("verified %d/%d scripts byte-identical" % (ok, len(snapshot)))
        for item in mismatches:
            print("  MISMATCH  %s" % item["rel"])
        for item in missing_local:
            print("  MISSING   %s" % item["rel"])
        if not mismatches and not missing_local:
            print("  all tracked scripts agree on both sides")
    return 1 if (mismatches or missing_local) else 0


def cmd_backup(args) -> int:
    project = Project.discover()
    logger = get_logger(project, args)
    with make_client(project) as client:
        result = backup_mod.backup_all(client, project.root, label=args.label)
    logger.info("studio backup: %d scripts -> %s", result["studio"]["scriptCount"], result["studio"]["dir"])
    logger.info("local  backup: %d files -> %s", result["local"]["files"], result["local"]["dir"])
    if args.json:
        _print(result)
    else:
        print("studio: %d scripts (%d bytes) -> %s" % (
            result["studio"]["scriptCount"], result["studio"]["totalBytes"], result["studio"]["dir"]))
        print("local : %d files -> %s" % (result["local"]["files"], result["local"]["dir"]))
    return 0


# --------------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return False
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rss", description="RobloxStudioLive - Studio <-> local live sync")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("status", help="connection and sync status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("import", help="first-time Studio -> local import")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("sync", help="run one sync cycle")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--pull-only", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("watch", help="run the live daemon (default)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--pull-only", action="store_true")
    p.add_argument("--cycles", type=int, default=0, help="run N cycles then exit")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("conflicts", help="list unresolved items")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_conflicts)

    p = sub.add_parser("resolve", help="resolve one conflict explicitly")
    p.add_argument("--path", required=True)
    p.add_argument("--take", required=True, choices=["studio", "local"])
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("diff", help="diff one script between the two sides")
    p.add_argument("--path", required=True)
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("tree", help="dump the Studio instance tree")
    p.add_argument("--root", default="game")
    p.add_argument("--depth", type=int, default=3)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("verify", help="check both sides agree")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("backup", help="timestamped backup of both sides")
    p.add_argument("label", nargs="?", default="backup")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_backup)

    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv[1:])
    if not args.command:
        args = parser.parse_args(["watch"])
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print("error: %s" % exc)
        return 2
    except StudioMCPError as exc:
        print("Studio MCP error: %s" % str(exc)[:400])
        return 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
