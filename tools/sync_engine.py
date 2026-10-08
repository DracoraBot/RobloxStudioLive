"""
RobloxStudioLive - synchronisation decision logic.

This module is deliberately free of MCP, filesystem-watching and network code:
it takes three plain inputs and returns a plan.  That makes the rules that
protect the user's code -- conflict detection, deletion refusal, loop
prevention -- directly unit-testable without Studio running (see
`tests/test_sync_engine.py`).

The three inputs
----------------
studio : {studio_path: {"className", "fnv", "bytes"}}      (from a poll)
local  : {rel_path:    {"sha256", "bytes", "mtime"}}       (from the disk)
state  : the previous sync state                            (the baseline)

Baseline model
--------------
`B` is the content hash at the last successful sync.  Comparing all three
(`S` studio now, `L` local now, `B` baseline) is what lets us tell "the user
edited this" apart from "we wrote this a moment ago" -- which is what stops a
two-way sync from ping-ponging forever.

Decision table
--------------
    S absent, L absent                -> nothing
    S present, L absent, B absent     -> pull          (new script in Studio)
    S present, L absent, B present    -> local-missing (flag; never delete in Studio)
    S absent,  L present, B absent    -> create        (new local file -> Studio)
    S absent,  L present, B present   -> studio-missing / rename (never delete local)
    S == L                            -> converged     (refresh baseline, no I/O)
    S != L, S == B                    -> push          (local edited)
    S != L, L == B                    -> pull          (Studio edited)
    S != L, S != B, L != B            -> conflict      (flag; overwrite nothing)

Deletions are never propagated in either direction.  A rename in Studio is
detected by matching content hashes and is applied as a *move*, with the
previous file preserved under backups/renames/ rather than discarded.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "SCRIPT_EXT",
    "Decision",
    "SyncState",
    "rel_path_for",
    "parse_local_path",
    "canonical",
    "sha256",
    "local_index",
    "decide",
    "plan",
    "summarise",
]

# Rojo-compatible suffixes; they keep the script class recoverable from the
# filename alone, which is essential for rebuilding a Script/LocalScript/
# ModuleScript correctly when pushing back to Studio.
SCRIPT_EXT = {
    "Script": ".server.luau",
    "LocalScript": ".client.luau",
    "ModuleScript": ".luau",
}
EXT_TO_CLASS = {v: k for k, v in SCRIPT_EXT.items()}
ALL_SUFFIXES = tuple(sorted(SCRIPT_EXT.values(), key=len, reverse=True))

DEFAULT_IGNORES = [
    "*.conflict-*.luau",
    "*.conflict-*.lua",
    "*.bak",
    "*.tmp",
    "*.orig",
    "~$*",
]


# --------------------------------------------------------------------------
# path mapping
# --------------------------------------------------------------------------
def rel_path_for(studio_path: str, class_name: str) -> str:
    """'ServerScriptService.Foo' + Script -> 'ServerScriptService/Foo.server.luau'."""
    suffix = SCRIPT_EXT.get(class_name, ".luau")
    parts = [p for p in studio_path.split(".") if p]
    if not parts:
        raise ValueError("empty studio path")
    parts[-1] = parts[-1] + suffix
    return "/".join(parts)


def parse_local_path(rel_path: str) -> tuple[str, str] | None:
    """'ServerScriptService/Foo.server.luau' -> ('ServerScriptService.Foo','Script').

    Returns None for anything that is not a mapped script file.
    """
    rel = rel_path.replace("\\", "/").lstrip("./")
    if not rel.endswith(".luau"):
        return None
    for suffix in ALL_SUFFIXES:
        if rel.endswith(suffix):
            class_name = EXT_TO_CLASS[suffix]
            stem = rel[: -len(suffix)]
            break
    else:
        class_name = "ModuleScript"
        stem = rel[:-len(".luau")]
    parts = [p for p in stem.split("/") if p]
    if not parts:
        return None
    return ".".join(parts), class_name


# --------------------------------------------------------------------------
# content
# --------------------------------------------------------------------------
def canonical(text: str) -> str:
    """Line-ending-normalised text.

    Roblox stores `Source` with LF endings and the bridge JSON-encodes strings
    faithfully, but OneDrive and Windows editors can introduce CRLF.  Hashing a
    single canonical form on both sides is what stops a CRLF round trip from
    looking like an endless edit.
    """
    if "\r" in text:
        return text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def sha256(text: str) -> str:
    return hashlib.sha256(canonical(text).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# local side
# --------------------------------------------------------------------------
def local_index(src_root: Path, ignores: Iterable[str] | None = None) -> dict[str, dict]:
    """Hash every mapped script file under `src_root`."""
    root = Path(src_root)
    patterns = list(DEFAULT_IGNORES) + list(ignores or [])
    index: dict[str, dict] = {}
    if not root.is_dir():
        return index

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if not name.endswith(".luau"):
                continue
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            if any(fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat) for pat in patterns):
                continue
            try:
                text = full.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            stat = full.stat()
            index[rel] = {
                "sha256": sha256(text),
                "bytes": len(canonical(text).encode("utf-8")),
                "mtime": stat.st_mtime,
                "text": canonical(text),
            }
    return index


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------
@dataclass
class SyncState:
    """Persisted baseline.  One entry per Studio script path."""

    path: Path
    place_id: int | None = None
    entries: dict[str, dict] = field(default_factory=dict)
    unresolved: dict[str, dict] = field(default_factory=dict)
    version: int = 1
    _dirty: bool = field(default=False, repr=False, compare=False)

    @classmethod
    def load(cls, path: Path) -> "SyncState":
        file = Path(path)
        if not file.is_file():
            return cls(path=file)
        try:
            raw = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(path=file)
        return cls(
            path=file,
            place_id=raw.get("placeId"),
            entries=dict(raw.get("entries") or {}),
            unresolved=dict(raw.get("unresolved") or {}),
            version=int(raw.get("version") or 1),
        )

    def save_if_dirty(self) -> bool:
        """Persist only when something actually changed.

        The daemon polls every couple of seconds; rewriting a 30 KB state file
        on every idle cycle would be pointless disk and OneDrive churn.
        """
        if not self._dirty:
            return False
        self.save()
        return True

    def save(self) -> None:
        payload = {
            "version": self.version,
            "placeId": self.place_id,
            "updatedAt": _now(),
            "entryCount": len(self.entries),
            "entries": self.entries,
            "unresolved": self.unresolved,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: a crash never leaves a half state file
        self._dirty = False

    def get(self, studio_path: str) -> dict:
        return self.entries.get(studio_path) or {}

    def baseline(self, studio_path: str) -> str | None:
        return self.get(studio_path).get("sha256")

    def record(
        self,
        studio_path: str,
        *,
        class_name: str,
        rel: str,
        content_hash: str,
        studio_fnv: str | None,
        direction: str,
        status: str = "synced",
    ) -> None:
        self.entries[studio_path] = {
            "className": class_name,
            "rel": rel,
            "sha256": content_hash,
            "studioFnv": studio_fnv,
            "lastSyncedAt": _now(),
            "lastDirection": direction,
            "status": status,
        }
        self.unresolved.pop(studio_path, None)
        self._dirty = True

    def mark(self, studio_path: str, status: str, detail: str = "",
             extra: dict | None = None) -> None:
        entry = self.entries.setdefault(studio_path, {})
        entry["status"] = status
        if detail:
            entry["detail"] = detail
            entry["flaggedAt"] = _now()
        if extra:
            entry.update(extra)
        self.unresolved[studio_path] = {"status": status, "detail": detail, "at": _now()}
        self._dirty = True

    def forget(self, studio_path: str) -> None:
        self.entries.pop(studio_path, None)
        self.unresolved.pop(studio_path, None)
        self._dirty = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# decisions
# --------------------------------------------------------------------------
@dataclass
class Decision:
    """One planned action.  `kind` drives what the daemon does next."""

    kind: str
    studio_path: str
    rel: str
    class_name: str
    reason: str
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


# kinds that move bytes and therefore deserve a log line
WRITE_KINDS = {"pull", "push", "create", "rename"}


def decide(
    studio_path: str,
    studio: dict | None,
    local_rel: str | None,
    local: dict | None,
    entry: dict | None,
) -> Decision:
    """Resolve one script path into a single action."""
    class_name = (
        (studio or {}).get("className")
        or (entry or {}).get("className")
        or (parse_local_path(local_rel)[1] if local_rel else "ModuleScript")
    )
    rel = local_rel or (entry or {}).get("rel") or rel_path_for(studio_path, class_name)

    S = sha256_write = None
    if studio is not None:
        S = studio.get("sha256")
    L = local.get("sha256") if local else None
    B = (entry or {}).get("sha256")

    # --- nothing on either side ------------------------------------------
    if studio is None and local is None:
        return Decision("nothing", studio_path, rel, class_name, "absent on both sides")

    # --- only Studio has it ----------------------------------------------
    if studio is not None and local is None:
        info = {"studioSha": S, "studioFnv": (studio or {}).get("fnv")}
        if B is None:
            return Decision("pull", studio_path, rel, class_name, "new in studio", info)
        return Decision(
            "local-missing", studio_path, rel, class_name,
            "local file is gone; studio copy left untouched",
            {"baseline": B},
        )

    # --- only local has it -----------------------------------------------
    if studio is None and local is not None:
        if B is None:
            return Decision("create", studio_path, rel, class_name,
                            "local file has no studio counterpart",
                            {"localSha": L})
        return Decision(
            "studio-missing", studio_path, rel, class_name,
            "gone from studio (deleted or renamed); local file left untouched",
            {"baseline": B, "localSha": L},
        )

    # --- both sides exist -------------------------------------------------
    if S is not None and S == L:
        if S == B:
            return Decision("converged", studio_path, rel, class_name, "already in sync")
        return Decision("converged", studio_path, rel, class_name,
                        "both sides identical; baseline refreshed", {"adopt": True})

    if B is None:
        return Decision("conflict", studio_path, rel, class_name,
                        "both sides exist with different content and no baseline",
                        {"studioSha": S, "localSha": L})

    studio_changed = S != B
    local_changed = L != B
    info = {"studioSha": S, "studioFnv": (studio or {}).get("fnv"), "localSha": L}

    if studio_changed and not local_changed:
        return Decision("pull", studio_path, rel, class_name, "studio changed", info)
    if local_changed and not studio_changed:
        return Decision("push", studio_path, rel, class_name, "local changed", info)
    if studio_changed and local_changed:
        return Decision("conflict", studio_path, rel, class_name,
                        "both sides changed since the last sync",
                        {"studioSha": S, "localSha": L, "baseline": B})
    return Decision("converged", studio_path, rel, class_name, "no change")


def plan(
    studio_snapshot: dict[str, dict],
    local: dict[str, dict],
    state: SyncState,
    propagate_renames: bool = True,
) -> list[Decision]:
    """Full decision list for one sync cycle.

    `local` is the index from `local_index` (keyed by rel path).  Studio hashes
    are filled in by the caller from the authoritative source text before this
    is called; the poll only supplies a cheap change detector.
    """
    decisions: list[Decision] = []
    handled: set[str] = set()

    # A class change in Studio (ModuleScript -> Script) yields a different
    # filename suffix, so the old path looks "gone locally" and the new one
    # "gone in Studio" at the same time -- nothing would ever be written and
    # the script would silently stop syncing in both directions.  Detect it
    # up front and emit a single retype that moves the file.
    retyped: dict[str, Decision] = {}
    for studio_path, meta in studio_snapshot.items():
        entry = state.get(studio_path)
        if not entry:
            continue
        new_class = meta.get("className")
        old_class = entry.get("className")
        if not new_class or not old_class or new_class == old_class:
            continue
        old_rel = entry.get("rel")
        new_rel = meta.get("rel") or rel_path_for(studio_path, new_class)
        if not old_rel or old_rel == new_rel:
            continue
        retyped[studio_path] = Decision(
            "retype", studio_path, new_rel, new_class,
            "script class changed in studio (%s -> %s)" % (old_class, new_class),
            {"fromRel": old_rel, "toRel": new_rel, "fromClass": old_class,
             "toClass": new_class},
        )
        handled.add(old_rel)
        handled.add(new_rel)

    for studio_path in studio_snapshot:
        if studio_path in retyped:
            continue
        meta = studio_snapshot[studio_path]
        rel = meta.get("rel") or rel_path_for(studio_path, meta.get("className", "ModuleScript"))
        decisions.append(
            decide(studio_path, meta, rel, local.get(rel), state.get(studio_path))
        )
        handled.add(rel)

    # local files with no studio counterpart
    for rel, meta in local.items():
        if rel in handled:
            continue
        parsed = parse_local_path(rel)
        if not parsed:
            continue
        studio_path, class_name = parsed
        decisions.append(
            decide(studio_path, None, rel, meta, state.get(studio_path))
        )

    decisions.extend(retyped.values())

    if propagate_renames:
        decisions = _apply_rename_detection(decisions)

    return decisions


def _apply_rename_detection(decisions: list[Decision]) -> list[Decision]:
    """Turn `studio-missing` + `pull` with identical content into one `rename`.

    Only applied when the match is unambiguous: exactly one vanished path and
    exactly one new path share a content hash.  Anything else is left as a
    delete-plus-create for the user to resolve, because guessing here could
    move the wrong file.
    """
    vanished = [d for d in decisions if d.kind == "studio-missing"]
    appeared = [d for d in decisions if d.kind == "pull" and d.reason == "new in studio"]
    if not vanished or not appeared:
        return decisions

    used: set[str] = set()
    consumed: set[int] = set()
    out = list(decisions)

    for gone in vanished:
        candidates = [
            (i, new) for i, new in enumerate(appeared)
            if new.studio_path not in used
            and new.detail.get("studioSha")
            and new.detail.get("studioSha") == gone.detail.get("localSha")
        ]
        if len(candidates) != 1:
            continue
        index, new = candidates[0]
        if index in consumed:
            continue
        consumed.add(index)
        used.add(new.studio_path)
        out[out.index(gone)] = Decision(
            "rename",
            new.studio_path,
            new.rel,
            new.class_name,
            "renamed or moved in studio",
            {
                "fromStudio": gone.studio_path,
                "fromRel": gone.rel,
                "toStudio": new.studio_path,
                "toRel": new.rel,
                "sha256": new.detail.get("studioSha"),
                "studioFnv": new.detail.get("studioFnv"),
            },
        )
        out.remove(new)

    return out


def summarise(decisions: list[Decision]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for decision in decisions:
        counts[decision.kind] = counts.get(decision.kind, 0) + 1
    return dict(sorted(counts.items()))
