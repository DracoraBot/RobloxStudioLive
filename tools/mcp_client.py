"""
RobloxStudioLive - MCP transport client.

Speaks the Model Context Protocol over stdio to Roblox's own `StudioMCP.exe`
(the same binary Roblox Studio ships at
%LOCALAPPDATA%\\Roblox\\Versions\\<version>\\StudioMCP.exe).

Why this exists
---------------
The Roblox Studio MCP server is a *request/response* bridge: it exposes tools
but provides no push notifications for "a script changed in Studio".  A live
sync daemon therefore needs (a) its own MCP connection that stays open across
many polls, and (b) a polling diff layer on top.  This module provides (a).

Design notes
------------
* Newline-delimited JSON-RPC 2.0 (that is what StudioMCP.exe speaks on stdio).
* A single reader thread demultiplexes responses to waiting callers by id.
* Every public call goes through `_request`, which transparently restarts the
  child process once and retries -- this is the "automatically reconnect after
  connection interruptions" requirement.
* No third-party dependencies.

Public API (frozen -- see docs/ARCHITECTURE.md):
    find_studio_mcp_exe() -> str
    broker_health(url=None) -> dict | None

    StudioMCPError
      StudioNotFoundError
      StudioMCPTimeout

    StudioMCPClient(exe_path=None, datamodel="Edit", timeout=120.0)
        .start() / .stop() / .restart()
        .alive            -> bool
        .list_studios()   -> list[dict]            # [{"id":..,"name":..}]
        .default_studio_id -> str | None
        .call_tool(name, arguments) -> Any         # decoded result payload
        .execute_luau(code, studio_id=None, datamodel_type=None) -> Any
        .ping() -> dict                            # connection self-test
        context manager support
"""

from __future__ import annotations

import glob
import itertools
import json
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

__all__ = [
    "StudioMCPError",
    "StudioNotFoundError",
    "StudioMCPTimeout",
    "find_studio_mcp_exe",
    "broker_health",
    "StudioMCPClient",
]

DEFAULT_BROKER_HEALTH = "http://127.0.0.1:13469/health"
PROTOCOL_VERSION = "2024-11-05"


# --------------------------------------------------------------------------
# exceptions
# --------------------------------------------------------------------------
class StudioMCPError(RuntimeError):
    """Any failure talking to Roblox Studio through the MCP bridge."""


class StudioNotFoundError(StudioMCPError):
    """StudioMCP.exe is missing, or no Studio instance is attached."""


class StudioMCPTimeout(StudioMCPError):
    """A request did not receive a response in time."""


# --------------------------------------------------------------------------
# discovery helpers
# --------------------------------------------------------------------------
def find_studio_mcp_exe() -> str:
    """Locate Roblox's bundled StudioMCP.exe.

    Resolution order:
      1. $RSS_STUDIOMCP_EXE
      2. config/settings.json -> mcp.exePath  (passed in by callers)
      3. newest %LOCALAPPDATA%/Roblox/Versions/*/StudioMCP.exe
    """
    env = os.environ.get("RSS_STUDIOMCP_EXE")
    if env and Path(env).is_file():
        return str(Path(env))

    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    versions = Path(local) / "Roblox" / "Versions"
    candidates = [Path(p) for p in glob.glob(str(versions / "*" / "StudioMCP.exe"))]
    if not candidates:
        raise StudioNotFoundError(
            "StudioMCP.exe not found. Looked in %s. "
            "Set RSS_STUDIOMCP_EXE to override." % versions
        )
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return str(candidates[0])


def broker_health(url: str | None = None, timeout: float = 2.5) -> dict | None:
    """Read the Studio MCP broker's /health endpoint.

    Returns e.g. {"ok": True, "studios": 1, "proxies": 5, "tools_cached": 27}
    or None when the broker is not reachable.
    """
    target = url or DEFAULT_BROKER_HEALTH
    try:
        with urllib.request.urlopen(target, timeout=timeout) as resp:  # noqa: S310
            body = resp.read().decode("utf-8", "replace")
    except Exception:
        return None

    info: dict[str, Any] = {"ok": True, "raw": body.strip()}
    for line in body.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower().replace(" ", "_")
        val = val.strip()
        if key in ("studios", "proxies"):
            try:
                info[key] = int(val)
            except ValueError:
                info[key] = val
        elif key.startswith("tools"):
            try:
                info["tools_cached"] = int(val)
            except ValueError:
                info["tools_cached"] = val
        else:
            info[key] = val
    return info


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------
class StudioMCPClient:
    """A persistent MCP session against Roblox Studio."""

    def __init__(
        self,
        exe_path: str | None = None,
        datamodel: str = "Edit",
        timeout: float = 120.0,
        client_name: str = "RobloxStudioLive",
        client_version: str = "1.0.0",
    ) -> None:
        self.exe_path = exe_path or find_studio_mcp_exe()
        self.datamodel = datamodel
        self.timeout = float(timeout)
        self.client_name = client_name
        self.client_version = client_version

        self._proc: subprocess.Popen | None = None
        # _lifecycle_lock guards start/stop/restart.  _lock guards only the
        # short critical sections around _pending -- it must NEVER be held
        # across a request/response round trip, or the reader thread cannot
        # demultiplex the reply and every call deadlocks until timeout.
        self._lifecycle_lock = threading.RLock()
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self._stderr_tail: list[str] = []
        self._stdout_tail: list[str] = []
        self._tools: list[dict] | None = None
        self._studios: list[dict] | None = None
        self._default_studio_id: str | None = None
        self._closing = False
        self._server_info: dict[str, Any] = {}

    # -- lifecycle ---------------------------------------------------------
    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> "StudioMCPClient":
        with self._lifecycle_lock:
            if self.alive:
                return self
            if not Path(self.exe_path).is_file():
                raise StudioNotFoundError("StudioMCP.exe not found at %s" % self.exe_path)
            self._closing = False
            self._stderr_tail.clear()
            self._stdout_tail.clear()
            self._proc = subprocess.Popen(  # noqa: S603
                [self.exe_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            threading.Thread(
                target=self._read_stdout, args=(self._proc,), daemon=True
            ).start()
            threading.Thread(
                target=self._read_stderr, args=(self._proc,), daemon=True
            ).start()

            result = self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": self.client_name, "version": self.client_version},
                },
            )
            self._server_info = (result or {}).get("serverInfo", {})
            self._notify("notifications/initialized", {})
            self._tools = None
            self._studios = None
            self._default_studio_id = None
            return self

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._closing = True
            proc = self._proc
            self._proc = None
            with self._lock:
                self._pending.clear()
        if proc is None:
            return
        try:
            if proc.poll() is None:
                if proc.stdin:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            pass

    def restart(self) -> "StudioMCPClient":
        self.stop()
        time.sleep(0.35)
        return self.start()

    def __enter__(self) -> "StudioMCPClient":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- background readers ------------------------------------------------
    def _read_stdout(self, proc: subprocess.Popen) -> None:
        if proc.stdout is None:
            return
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                self._stdout_tail.append(line[:2000])
                del self._stdout_tail[:-40]
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                mid = msg.get("id")
                if mid is None:
                    continue
                with self._lock:
                    slot = self._pending.get(mid)
                if slot is not None:
                    slot["response"] = msg
                    slot["event"].set()
        except Exception:
            pass
        finally:
            # A closed pipe means no reply is coming.  Only wake up waiters if
            # this is still the live process -- a stale reader from a previous
            # connection must not unblock requests on the current one.
            if self._proc is proc:
                with self._lock:
                    slots = list(self._pending.values())
                for slot in slots:
                    slot["event"].set()

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        if proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                self._stderr_tail.append(raw.decode("utf-8", "replace").rstrip()[:2000])
                del self._stderr_tail[:-40]
        except Exception:
            pass

    # -- raw JSON-RPC ------------------------------------------------------
    def _write(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise StudioMCPError("StudioMCP process is not running")
        blob = (json.dumps(payload) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                proc.stdin.write(blob)
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise StudioMCPError("failed writing to StudioMCP stdin: %s" % exc) from exc

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        mid = next(self._ids)
        slot: dict[str, Any] = {"event": threading.Event(), "response": None}
        with self._lock:
            self._pending[mid] = slot
        try:
            self._write({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
            wait = self.timeout if timeout is None else float(timeout)
            if not slot["event"].wait(wait):
                raise StudioMCPTimeout(
                    "no response to %s within %.1fs%s"
                    % (method, wait, self._diag())
                )
            msg = slot["response"]
            if msg is None:
                raise StudioMCPError("connection closed while awaiting %s%s" % (method, self._diag()))
            if "error" in msg:
                err = msg["error"] or {}
                raise StudioMCPError("%s failed: %s" % (method, err.get("message", err)))
            return msg.get("result")
        finally:
            with self._lock:
                self._pending.pop(mid, None)

    def _request_with_reconnect(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        """Retry once behind a fresh connection -- 'reconnect after interruption'."""
        try:
            if not self.alive:
                self.restart()
            return self._request(method, params, timeout)
        except (StudioMCPError, OSError) as first:
            if self._closing:
                raise
            try:
                self.restart()
            except Exception as exc:
                raise StudioMCPError("%s; reconnect failed: %s" % (first, exc)) from first
            return self._request(method, params, timeout)

    def _diag(self) -> str:
        bits = []
        if self._stderr_tail:
            bits.append("stderr=" + " | ".join(self._stderr_tail[-3:]))
        if self._stdout_tail:
            bits.append("stdout=" + " | ".join(self._stdout_tail[-2:])[:400])
        return (" [" + "; ".join(bits) + "]") if bits else ""

    # -- MCP surface -------------------------------------------------------
    def list_tools(self, refresh: bool = False) -> list[dict]:
        if self._tools is None or refresh:
            result = self._request_with_reconnect("tools/list", {})
            self._tools = list((result or {}).get("tools") or [])
        return self._tools

    def tool_names(self) -> list[str]:
        return [t.get("name", "") for t in self.list_tools()]

    def list_studios(self, refresh: bool = False) -> list[dict]:
        """Attached Studio instances. Cached; pass refresh=True to re-query."""
        if self._studios is None or refresh:
            result = self.call_tool("list_roblox_studios", {}, retry=True)
            if isinstance(result, dict):
                studios = result.get("studios") or []
            elif isinstance(result, list):
                studios = result
            else:
                studios = []
            self._studios = [s for s in studios if isinstance(s, dict)]
            self._default_studio_id = self._studios[0].get("id") if self._studios else None
        return list(self._studios)

    @property
    def default_studio_id(self) -> str | None:
        if self._default_studio_id is None:
            self.list_studios(refresh=True)
        return self._default_studio_id

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        retry: bool = True,
    ) -> Any:
        params = {"name": name, "arguments": dict(arguments or {})}
        if retry:
            result = self._request_with_reconnect("tools/call", params)
        else:
            result = self._request("tools/call", params)
        return self._decode_tool_result(name, result)

    def _decode_tool_result(self, name: str, result: Any) -> Any:
        if not isinstance(result, dict):
            return result
        if result.get("isError"):
            raise StudioMCPError("tool %s reported an error: %s" % (name, self._flatten(result)))

        text = self._flatten(result)
        if text is None:
            return result

        stripped = text.strip()
        # The Studio proxy surfaces Luau/plugin failures as plain text.
        if stripped.startswith("Error:") or stripped.startswith("Error "):
            raise StudioMCPError("tool %s failed: %s" % (name, stripped[:1000]))
        # The ExecuteLuau tool substitutes this literal when a returned Luau
        # table exceeds ~90 KB.  Callers must never mistake it for data.
        if stripped == "[Complex Table]":
            raise StudioMCPError(
                "tool %s: response exceeded the bridge's table limit and was "
                "replaced by '[Complex Table]'; return a JSON string instead" % name
            )

        if stripped == "":
            # An empty payload is a legitimate empty-string result.  It must not
            # be confused with "the tool returned nothing" (no content list).
            return "" if text == "" else None
        try:
            return json.loads(stripped)
        except ValueError as exc:
            # The bridge truncates responses at ~100 000 characters.  A payload
            # that *looks* like JSON but does not parse has almost certainly
            # been cut in half -- surfacing that loudly is the whole point,
            # because a silently-short read could become a silently-short write.
            if stripped[0] in "{[\"":
                raise StudioMCPError(
                    "tool %s returned truncated or invalid JSON (%d chars): %s"
                    % (name, len(stripped), str(exc)[:120])
                ) from exc
            return stripped

    @staticmethod
    def _flatten(result: dict[str, Any]) -> str | None:
        content = result.get("content")
        if not isinstance(content, list):
            return None
        chunks = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        return "\n".join(chunks) if chunks else None

    # -- convenience -------------------------------------------------------
    def execute_luau(
        self,
        code: str,
        studio_id: str | None = None,
        datamodel_type: str | None = None,
    ) -> Any:
        args = {
            "code": code,
            "datamodel_type": datamodel_type or self.datamodel,
            "studio_id": studio_id or self.default_studio_id,
        }
        if not args["studio_id"]:
            raise StudioNotFoundError("no Roblox Studio instance is attached to the MCP bridge")
        return self.call_tool("execute_luau", args)

    def ping(self) -> dict:
        """Cheap round-trip that proves the MCP session is usable."""
        started = time.time()
        try:
            info = self.execute_luau('return {ok = true, n = 1 + 1, name = game.Name}')
            return {
                "connected": True,
                "latencyMs": round((time.time() - started) * 1000.0, 1),
                "studio": info,
                "server": self._server_info,
                "tools": len(self.list_tools()),
                "broker": broker_health(),
            }
        except StudioMCPError as exc:
            return {"connected": False, "error": str(exc), "broker": broker_health()}


# --------------------------------------------------------------------------
# manual smoke test:  python tools/mcp_client.py
# --------------------------------------------------------------------------
if __name__ == "__main__":
    print("StudioMCP.exe :", find_studio_mcp_exe())
    print("broker /health:", broker_health())
    with StudioMCPClient() as client:
        print("tools         :", len(client.list_tools()))
        print("studios       :", client.list_studios(refresh=True))
        import pprint

        pprint.pprint(client.ping())
