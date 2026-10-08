"""
RobloxStudioLive - project layout and settings.

`Project.discover()` walks up from a starting directory looking for
config/settings.json, so every tool behaves the same no matter which directory
it is launched from.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Project", "DEFAULT_SETTINGS"]

DEFAULT_SETTINGS: dict = {
    "version": 1,
    "roots": [
        "ServerScriptService", "ReplicatedStorage", "StarterGui",
        "StarterPlayer", "ReplicatedFirst", "ServerStorage", "Workspace",
    ],
    "sync": {
        "pollIntervalSeconds": 2.0,
        "settleSeconds": 1.5,
        "autoPull": True,
        "autoPush": True,
        "propagateRenames": True,
        "ignoreLocalGlobs": ["*.conflict-*.luau", "*.bak", "*.tmp", "~$*"],
    },
    "logging": {"level": "INFO"},
    "mcp": {"exePath": None, "datamodel": "Edit", "requestTimeoutSeconds": 120},
}

# Deliberately NOT configurable, and therefore absent from DEFAULT_SETTINGS:
#   * propagating deletions in either direction -- the daemon has no delete
#     path at all, so there is no switch to get wrong.
#   * the conflict policy -- conflicts always park a copy and overwrite
#     nothing.
# Advertising a switch that no code reads is worse than having no switch, so
# these were removed rather than left inert.


@dataclass
class Project:
    root: Path

    # -- discovery ---------------------------------------------------------
    @classmethod
    def discover(cls, start: str | Path | None = None) -> "Project":
        candidates: list[Path] = []
        if start is not None:
            candidates.append(Path(start).resolve())
        else:
            candidates.append(Path.cwd().resolve())
            # also try relative to this file, so the CLI works from anywhere
            candidates.append(Path(__file__).resolve().parent.parent)

        for origin in candidates:
            for candidate in [origin, *origin.parents]:
                if (candidate / "config" / "settings.json").is_file():
                    return cls(candidate)
        raise FileNotFoundError(
            "no RobloxStudioLive project found at or above %s "
            "(expected config/settings.json)" % candidates[0]
        )

    # -- paths -------------------------------------------------------------
    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def src_dir(self) -> Path:
        return self.root / "src"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def settings_path(self) -> Path:
        return self.config_dir / "settings.json"

    @property
    def state_path(self) -> Path:
        return self.root / "sync-state.json"

    @property
    def lock_path(self) -> Path:
        return self.root / ".rss-daemon.lock"

    @property
    def structure_path(self) -> Path:
        return self.config_dir / "structure.json"

    # -- settings ----------------------------------------------------------
    @property
    def settings(self) -> dict:
        merged = json.loads(json.dumps(DEFAULT_SETTINGS))  # deep copy
        try:
            loaded = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        _deep_merge(merged, loaded)
        return merged

    @property
    def sync_settings(self) -> dict:
        return self.settings.get("sync", {})

    @property
    def roots(self) -> list[str]:
        return list(self.settings.get("roots") or DEFAULT_SETTINGS["roots"])

    def ensure_dirs(self) -> None:
        for directory in (self.src_dir, self.backups_dir, self.logs_dir,
                          self.config_dir, self.root / "docs"):
            directory.mkdir(parents=True, exist_ok=True)


def _deep_merge(base: dict, overlay: dict) -> None:
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
