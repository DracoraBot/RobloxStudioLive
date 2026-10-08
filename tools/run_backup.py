"""
RobloxStudioLive - backup command.

    python tools/run_backup.py [label]

Creates backups/<label>_studio_<utc>/ with a read-only dump of every Studio
script, plus backups/<label>_local_<utc>/ with the current local src/, config/
and sync-state.json.  Refuses to reuse an existing directory, so no previous
backup is ever overwritten.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import backup  # noqa: E402
import rslog  # noqa: E402
from mcp_client import StudioMCPClient, broker_health, find_studio_mcp_exe  # noqa: E402
from project import Project  # noqa: E402


def main(argv: list[str]) -> int:
    label = argv[1] if len(argv) > 1 else "backup"
    project = Project.discover(__file__)
    logger = rslog.get_logger(project.logs_dir)

    settings = project.settings
    logger.info("backup: StudioMCP=%s", find_studio_mcp_exe())
    logger.info("backup: broker=%s", broker_health())

    with StudioMCPClient(
        exe_path=(settings.get("mcp") or {}).get("exePath"),
        datamodel=(settings.get("mcp") or {}).get("datamodel", "Edit"),
        timeout=float((settings.get("mcp") or {}).get("requestTimeoutSeconds", 120)),
    ) as client:
        result = backup.backup_all(client, project.root, label=label)

    studio = result["studio"]
    local = result["local"]
    logger.info("studio backup: %d scripts, %d bytes -> %s",
                studio["scriptCount"], studio["totalBytes"], studio["dir"])
    logger.info("local  backup: %d files -> %s", local["files"], local["dir"])
    rslog.audit(project.logs_dir, {"event": "backup", "studio": studio, "local": local})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
