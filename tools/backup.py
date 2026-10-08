"""
RobloxStudioLive - backups.

Two independent things are backed up, both non-destructively:

* `backup_studio(...)` reads every script out of Studio and writes a full
  timestamped snapshot plus a JSON manifest of hashes.  This is read-only with
  respect to Studio.
* `backup_local(...)` copies the current `src/`, `config/` and `sync-state.json`
  into the same snapshot so a local mistake is equally recoverable.

Nothing here deletes or overwrites: a new timestamped directory is created for
every run, and an existing directory is never reused.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["backup_studio", "backup_local", "backup_all", "latest_backup"]


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")


def _unique_dir(base: Path) -> Path:
    candidate = base
    suffix = 1
    while candidate.exists():
        candidate = base.with_name("%s_%d" % (base.name, suffix))
        suffix += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def backup_studio(
    client: Any,
    backups_dir: Path,
    roots: list[str] | None = None,
    label: str = "studio",
) -> dict:
    """Read-only snapshot of every Studio script.  Never writes to Studio."""
    import studio_ops  # local import keeps this module importable without a bridge

    dest = _unique_dir(Path(backups_dir) / ("%s_%s" % (label, _stamp())))
    scripts_dir = dest / "studio-scripts"

    snapshot = studio_ops.snapshot_scripts(client, roots)
    sizes = {p: m.get("bytes", 0) for p, m in snapshot.items()}
    sources = studio_ops.fetch_sources(client, list(snapshot), sizes=sizes)

    manifest = {
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "kind": label,
        "scriptCount": len(snapshot),
        "totalBytes": sum(len(s.encode("utf-8")) for s in sources.values()),
        "scripts": [],
    }

    for path in sorted(snapshot):
        meta = snapshot[path]
        source = sources.get(path, "")
        rel = _backup_rel(path, meta.get("className", "ModuleScript"))
        target = scripts_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_text(target, source)
        manifest["scripts"].append({
            "studioPath": path,
            "className": meta.get("className"),
            "rel": str(rel).replace("\\", "/"),
            "bytes": len(source.encode("utf-8")),
            "sha256": _sha256(source),
            "studioFnv": meta.get("fnv"),
        })

    _write_text(dest / "manifest.json", json.dumps(manifest, indent=2))
    return {
        "dir": str(dest),
        "scriptCount": manifest["scriptCount"],
        "totalBytes": manifest["totalBytes"],
    }


def backup_local(project_root: Path, backups_dir: Path, label: str = "local") -> dict:
    """Copy src/, config/ and sync-state.json into a timestamped snapshot."""
    root = Path(project_root)
    dest = _unique_dir(Path(backups_dir) / ("%s_%s" % (label, _stamp())))
    copied = 0

    for name in ("src", "config"):
        source = root / name
        if source.is_dir():
            target = dest / name
            shutil.copytree(source, target)
            copied += sum(1 for _ in target.rglob("*") if _.is_file())

    state = root / "sync-state.json"
    if state.is_file():
        shutil.copy2(state, dest / "sync-state.json")
        copied += 1

    _write_text(
        dest / "manifest.json",
        json.dumps({
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "kind": label,
            "files": copied,
        }, indent=2),
    )
    return {"dir": str(dest), "files": copied}


def backup_all(client: Any, project_root: Path, label: str = "pre-sync") -> dict:
    """Full both-sides backup -- call this before enabling live sync."""
    root = Path(project_root)
    backups = root / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    studio = backup_studio(client, backups, label="%s_studio" % label)
    local = backup_local(root, backups, label="%s_local" % label)
    return {"studio": studio, "local": local}


def latest_backup(backups_dir: Path) -> str | None:
    root = Path(backups_dir)
    if not root.is_dir():
        return None
    dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name)
    return str(dirs[-1]) if dirs else None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _backup_rel(studio_path: str, class_name: str) -> Path:
    """Mirror the Studio hierarchy, using the project's script suffix scheme."""
    import sync_engine

    return Path(sync_engine.rel_path_for(studio_path, class_name))


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_text(path: Path, text: str) -> None:
    """UTF-8, LF, no BOM -- identical bytes on every platform."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
