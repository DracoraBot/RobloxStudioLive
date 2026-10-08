"""
Deterministic safety acceptance tests.

Run with the daemon STOPPED (`rss watch` must not be running) so each test can
drive exactly one sync cycle and observe the result.

Every test creates its own scratch script and cleans up after itself; the user's
existing scripts are never modified.  A final integrity check confirms the place
still matches the state recorded at the start.

    python tests/acceptance_safety.py

Covers MVP criteria:
    * an AI-authored local script can be created in Studio
    * conflicting edits are flagged, never silently destroyed
    * deletions are never propagated in either direction
    * renames/moves are detected and applied as moves
    * unrelated files stay untouched
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import studio_ops  # noqa: E402
import sync_engine  # noqa: E402
from mcp_client import StudioMCPClient  # noqa: E402

PY = sys.executable
PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, (" -- " + detail) if detail else ""))


def rss(*args: str) -> dict:
    """Run one rss command and return its parsed --json output."""
    proc = subprocess.run(
        [PY, str(ROOT / "tools" / "rss.py"), *args, "--json"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300,
    )
    text = (proc.stdout or "").strip()
    start = text.find("{")
    if start >= 0:
        try:
            return json.loads(text[start:])
        except ValueError:
            pass
    return {"_raw": text, "_stderr": proc.stderr[-400:], "_rc": proc.returncode}


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


def make_scratch(client, parent: str, name: str, cls: str, body: str) -> str:
    full = parent + "." + name
    if studio_ops.script_exists(client, full):
        studio_ops.delete_script(client, full, ops_token())
    result = studio_ops.create_script(client, parent, name, cls, body)
    assert result.get("ok"), result
    return full


def ops_token() -> str:
    return studio_ops.DELETE_CONFIRM_TOKEN


def cleanup(client, paths: list[str], rels: list[str]) -> None:
    for path in paths:
        try:
            if studio_ops.script_exists(client, path):
                studio_ops.delete_script(client, path, ops_token())
        except Exception:
            pass
    for rel in rels:
        target = ROOT / "src" / rel
        if target.is_file():
            target.unlink()
        for conflict in target.parent.glob(target.stem + ".conflict-*.luau"):
            conflict.unlink()


def reset_scratch(client) -> list[str]:
    """Remove every RSS_* artifact from Studio, src/ and sync-state.json.

    Called before and after the run so the suite is hermetic and repeatable: a
    leftover scratch file from a previous run would otherwise be planned as a
    legitimate create/rename target and skew the results.
    """
    removed = []
    for path in list(studio_ops.snapshot_scripts(client)):
        if "RSS_" in path:
            studio_ops.delete_script(client, path, ops_token())
            removed.append(path)

    for target in (ROOT / "src").rglob("RSS_*"):
        if target.is_file():
            target.unlink()
            removed.append(str(target.relative_to(ROOT)))

    state = sync_engine.SyncState.load(ROOT / "sync-state.json")
    for path in [p for p in state.entries if "RSS_" in p]:
        state.forget(path)
    for path in [p for p in state.unresolved if "RSS_" in p]:
        state.unresolved.pop(path, None)
    state.save()
    return removed


def main() -> int:
    print("=" * 74)
    print("SAFETY acceptance tests (daemon must be stopped)")
    print("=" * 74)

    with StudioMCPClient() as client:
        stale = reset_scratch(client)
        if stale:
            print("cleared %d leftover scratch artifact(s) from a previous run" % len(stale))
            subprocess.run([PY, str(ROOT / "tools" / "rss.py"), "sync"],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=300)

        baseline = studio_ops.snapshot_scripts(client)
        baseline_hashes = {p: m.get("fnv") for p, m in baseline.items()}
        print("baseline: %d scripts\n" % len(baseline))

        created_paths: list[str] = []
        created_rels: list[str] = []

        try:
            # ------------------------------------------------------------------
            print("-- 1. AI-authored local script -> Studio (create) --")
            rel = "ReplicatedStorage/RSS_InventoryDemo.luau"
            target = ROOT / "src" / rel
            body = (
                "-- Generated by RobloxStudioLive from a natural-language prompt.\n"
                "-- Creates a simple inventory UI inside StarterGui at runtime.\n"
                "local Players = game:GetService('Players')\n"
                "local ReplicatedStorage = game:GetService('ReplicatedStorage')\n\n"
                "return function()\n"
                "\tprint('[RSS] inventory demo loaded for', Players.LocalPlayer)\n"
                "\treturn { slots = 12, theme = 'dark gray' }\n"
                "end\n"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8", newline="")
            created_rels.append(rel)

            studio_path = "ReplicatedStorage.RSS_InventoryDemo"
            check("demo script does not exist in Studio yet",
                  not studio_ops.script_exists(client, studio_path))

            # A local file must settle before it is pushed, so an editor caught
            # mid-save never ships a truncated script to Studio.
            time.sleep(2.0)
            rss("sync")
            rss("sync")
            created_paths.append(studio_path)

            check("AI-authored local file was created in Studio",
                  studio_ops.script_exists(client, studio_path))
            if studio_ops.script_exists(client, studio_path):
                check("created Studio content is byte-exact",
                      get_studio(client, studio_path) == body)
                check("created with the right class",
                      studio_ops.snapshot_scripts(client)[studio_path]["className"] == "ModuleScript")
                check("local file was not rewritten by the push",
                      sync_engine.canonical(target.read_text(encoding="utf-8")) == body)

            # ------------------------------------------------------------------
            print("\n-- 2. conflicting edits are flagged, never destroyed --")
            conflict_rel = "ServerStorage/RSS_ConflictScratch.luau"
            conflict_path = "ServerStorage.RSS_ConflictScratch"
            conflict_target = ROOT / "src" / conflict_rel
            original = "-- conflict scratch\nreturn { v = 1 }\n"
            make_scratch(client, "ServerStorage", "RSS_ConflictScratch", "ModuleScript", original)
            created_paths.append(conflict_path)
            conflict_target.parent.mkdir(parents=True, exist_ok=True)
            conflict_target.write_text(original, encoding="utf-8", newline="")
            created_rels.append(conflict_rel)

            time.sleep(2.0)
            rss("sync")  # establish the baseline
            check("baseline established for the conflict scratch",
                  sync_engine.SyncState.load(ROOT / "sync-state.json").get(conflict_path).get("sha256")
                  == sync_engine.sha256(original))

            studio_version = "-- conflict scratch\nreturn { v = 'STUDIO EDIT' }\n"
            local_version = "-- conflict scratch\nreturn { v = 'LOCAL EDIT' }\n"
            set_studio(client, conflict_path, studio_version)
            conflict_target.write_text(local_version, encoding="utf-8", newline="")

            rss("sync")

            state = sync_engine.SyncState.load(ROOT / "sync-state.json")
            check("conflict was flagged in sync-state.json",
                  state.unresolved.get(conflict_path, {}).get("status") == "conflict",
                  str(state.unresolved.get(conflict_path)))
            check("LOCAL content survived the conflict untouched",
                  sync_engine.canonical(conflict_target.read_text(encoding="utf-8")) == local_version)
            check("STUDIO content survived the conflict untouched",
                  get_studio(client, conflict_path) == studio_version)

            conflict_files = sorted(conflict_target.parent.glob("*.conflict-*.luau"))
            check("a conflict copy of the Studio version was written", len(conflict_files) == 1,
                  str([f.name for f in conflict_files]))
            if conflict_files:
                saved = conflict_files[0].read_text(encoding="utf-8")
                check("conflict copy contains the Studio version", studio_version in saved)

            listed = rss("conflicts")
            check("rss conflicts reports it", any(
                item["studioPath"] == conflict_path for item in listed.get("unresolved", [])),
                str(listed)[:200])

            # resolve explicitly, then confirm both sides agree again
            print("      resolving with --take local ...")
            proc = subprocess.run(
                [PY, str(ROOT / "tools" / "rss.py"), "resolve",
                 "--path", conflict_path, "--take", "local"],
                cwd=str(ROOT), capture_output=True, text=True, timeout=300,
            )
            check("explicit resolve --take local succeeded", proc.returncode == 0,
                  proc.stdout.strip()[:120] or proc.stderr.strip()[:120])
            check("after resolve, Studio holds the local version",
                  get_studio(client, conflict_path) == local_version)
            check("conflict copy was cleaned up after resolving",
                  not list(conflict_target.parent.glob("*.conflict-*.luau")))

            # ------------------------------------------------------------------
            print("\n-- 3. deletions are never propagated --")
            del_rel = "ServerStorage/RSS_DeleteScratch.luau"
            del_path = "ServerStorage.RSS_DeleteScratch"
            del_target = ROOT / "src" / del_rel
            del_body = "-- delete scratch\nreturn {}\n"
            make_scratch(client, "ServerStorage", "RSS_DeleteScratch", "ModuleScript", del_body)
            created_paths.append(del_path)
            del_target.write_text(del_body, encoding="utf-8", newline="")
            created_rels.append(del_rel)
            time.sleep(2.0)
            rss("sync")
            check("delete scratch is tracked", del_target.is_file())

            # delete it inside Studio, the way a user would
            studio_ops.delete_script(client, del_path, ops_token())
            check("removed from Studio", not studio_ops.script_exists(client, del_path))

            rss("sync")
            check("LOCAL file was NOT deleted when Studio lost the script",
                  del_target.is_file())
            if del_target.is_file():
                check("local content is intact",
                      sync_engine.canonical(del_target.read_text(encoding="utf-8")) == del_body)
            state = sync_engine.SyncState.load(ROOT / "sync-state.json")
            check("flagged as missing-in-studio",
                  state.unresolved.get(del_path, {}).get("status") == "missing-in-studio",
                  str(state.unresolved.get(del_path)))

            # reverse direction: delete the local file, Studio must survive
            rev_rel = "ServerStorage/RSS_LocalDeleteScratch.luau"
            rev_path = "ServerStorage.RSS_LocalDeleteScratch"
            rev_body = "-- local delete scratch\nreturn { keep = true }\n"
            make_scratch(client, "ServerStorage", "RSS_LocalDeleteScratch", "ModuleScript", rev_body)
            created_paths.append(rev_path)
            rev_target = ROOT / "src" / rev_rel
            rev_target.write_text(rev_body, encoding="utf-8", newline="")
            created_rels.append(rev_rel)
            time.sleep(2.0)
            rss("sync")

            rev_target.unlink()
            rss("sync")
            check("STUDIO script was NOT deleted when the local file vanished",
                  studio_ops.script_exists(client, rev_path))
            check("studio content is intact", get_studio(client, rev_path) == rev_body)
            state = sync_engine.SyncState.load(ROOT / "sync-state.json")
            check("flagged as missing-locally",
                  state.unresolved.get(rev_path, {}).get("status") == "missing-locally",
                  str(state.unresolved.get(rev_path)))
            rev_target.write_text(rev_body, encoding="utf-8", newline="")

            # ------------------------------------------------------------------
            print("\n-- 4. rename/move in Studio is applied as a move --")
            ren_rel = "ServerStorage/RSS_RenameOld.luau"
            ren_path = "ServerStorage.RSS_RenameOld"
            ren_body = "-- rename scratch\nreturn { moved = true }\n"
            make_scratch(client, "ServerStorage", "RSS_RenameOld", "ModuleScript", ren_body)
            created_paths.append(ren_path)
            ren_target = ROOT / "src" / ren_rel
            ren_target.write_text(ren_body, encoding="utf-8", newline="")
            created_rels.append(ren_rel)
            time.sleep(2.0)
            rss("sync")
            check("rename scratch imported", ren_target.is_file())

            new_path = "ServerStorage.RSS_RenameNew"
            new_rel = "ServerStorage/RSS_RenameNew.luau"
            created_paths.append(new_path)
            # move inside Studio: create at the new name with identical content
            studio_ops.create_script(client, "ServerStorage", "RSS_RenameNew", "ModuleScript", ren_body)
            studio_ops.delete_script(client, ren_path, ops_token())

            rss("sync")
            new_target = ROOT / "src" / new_rel
            check("local file followed the Studio move",
                  new_target.is_file() and not ren_target.is_file(),
                  "new=%s old=%s" % (new_target.is_file(), ren_target.is_file()))
            if new_target.is_file():
                check("moved file kept its content",
                      sync_engine.canonical(new_target.read_text(encoding="utf-8")) == ren_body)
            preserved = list((ROOT / "backups" / "renames").rglob("RSS_RenameOld.luau"))
            check("previous path preserved under backups/renames/", bool(preserved),
                  str([str(p.relative_to(ROOT)) for p in preserved][:2]))

            # ------------------------------------------------------------------
            print("\n-- 5. unrelated files are untouched --")
            sentinel_dir = ROOT / "_sentinel_unrelated"
            sentinel_dir.mkdir(exist_ok=True)
            sentinel = sentinel_dir / "do-not-touch.txt"
            sentinel.write_text("unrelated content\n", encoding="utf-8")
            outside = Path(tempfile.mkdtemp(prefix="rss-outside-"))
            outside_file = outside / "outside.txt"
            outside_file.write_text("outside the project\n", encoding="utf-8")

            rss("sync")
            check("sentinel file inside the project is untouched",
                  sentinel.read_text(encoding="utf-8") == "unrelated content\n")
            check("file outside the project is untouched",
                  outside_file.read_text(encoding="utf-8") == "outside the project\n")
            check("sentinel was not imported as a script",
                  not (ROOT / "src" / "_sentinel_unrelated").exists())
            shutil.rmtree(sentinel_dir, ignore_errors=True)
            shutil.rmtree(outside, ignore_errors=True)
            check("sentinel directory removed again",
                  not sentinel_dir.exists())

        finally:
            print("\n-- cleanup --")
            cleanup(client, created_paths + ["ServerStorage.RSS_ConflictScratch"], created_rels)
            removed = reset_scratch(client)
            if removed:
                print("      removed %d scratch artifact(s)" % len(removed))
            subprocess.run([PY, str(ROOT / "tools" / "rss.py"), "sync"],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=300)

            final = studio_ops.snapshot_scripts(client)
            leftovers = [p for p in final if "RSS_" in p]
            check("all scratch scripts removed from Studio", not leftovers, str(leftovers))
            check("no scratch files left under src/",
                  not list((ROOT / "src").rglob("RSS_*")))
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
    raise SystemExit(main())
