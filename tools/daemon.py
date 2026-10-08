"""
RobloxStudioLive - the synchronisation daemon.

One `cycle()` is a full sync pass:

    1. poll Studio for script metadata + a cheap change-detector hash
    2. fetch full source only for scripts that actually changed
    3. hash the local tree
    4. ask sync_engine for a plan (pure logic, unit-tested offline)
    5. apply the plan, verifying every write in both directions
    6. persist the baseline so the next cycle settles instead of ping-ponging

Safety properties, all enforced here:

* Deletions are never propagated.  A script missing on one side is reported and
  left alone.
* A conflict writes the *other* version to a `.conflict-<ts>.luau` file and
  overwrites nothing.
* A write is only recorded as synced after it has been read back and verified.
* A local file must be stable for N consecutive cycles before it is pushed, so
  a half-written editor save is never sent to Studio.
* Studio writes are compare-and-swap: if Studio changed since we read it, the
  write is refused and becomes a conflict.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import rslog
import studio_ops
import sync_engine
from mcp_client import StudioMCPClient, StudioMCPError
from project import Project

__all__ = ["SyncDaemon", "SyncError"]


class SyncError(RuntimeError):
    """A sync step could not be completed safely."""


class SyncDaemon:
    def __init__(
        self,
        project: Project,
        client: StudioMCPClient,
        logger: Any,
        *,
        dry_run: bool = False,
        pull_only: bool = False,
        verify: bool = True,
    ) -> None:
        self.project = project
        self.client = client
        self.log = logger
        self.dry_run = dry_run
        self.pull_only = pull_only
        self.verify = verify

        self.state = sync_engine.SyncState.load(project.state_path)
        settings = project.sync_settings
        self.interval = float(settings.get("pollIntervalSeconds", 2.0))
        self.settle_seconds = float(settings.get("settleSeconds", 1.5))
        self.auto_pull = bool(settings.get("autoPull", True))
        self.auto_push = bool(settings.get("autoPush", True))
        self.propagate_renames = bool(settings.get("propagateRenames", True))
        self.ignores = list(settings.get("ignoreLocalGlobs") or [])

        self._stable: dict[str, tuple[str, int]] = {}
        self._stop = False

    # ------------------------------------------------------------------
    # public entry points
    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        """Loop forever, surviving Studio restarts."""
        backoff = 1.0
        self.log.info("watching: poll=%.1fs settle=%.1fs src=%s",
                      self.interval, self.settle_seconds, self.project.src_dir)
        while not self._stop:
            try:
                summary = self.cycle()
                if summary["changed"]:
                    self.log.info("cycle: %s", _fmt(summary))
                backoff = 1.0
                self._sleep(self.interval)
            except KeyboardInterrupt:
                self.log.info("stopping on interrupt")
                break
            except StudioMCPError as exc:
                self.log.warning("studio unreachable (%s); local files are safe, retrying in %.0fs",
                                 _short(exc), backoff)
                self._safely_reconnect()
                self._sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except Exception as exc:  # keep the daemon alive through surprises
                self.log.exception("cycle failed: %s", _short(exc))
                self._sleep(min(backoff, 10.0))
                backoff = min(backoff * 2, 30.0)
        self.log.info("daemon stopped")

    def cycle(self) -> dict:
        """One full sync pass.  Returns a summary dict."""
        started = time.time()
        summary = {
            "at": datetime.now(timezone.utc).isoformat(),
            "pulled": [], "pushed": [], "created": [], "renamed": [],
            "conflicts": [], "missingStudio": [], "missingLocal": [],
            "errors": [], "converged": 0, "changed": False,
        }

        snapshot = studio_ops.snapshot_scripts(self.client, self.project.roots)
        view = self._studio_view(snapshot)
        local = sync_engine.local_index(self.project.src_dir, self.ignores)

        decisions = sync_engine.plan(view, local, self.state,
                                     propagate_renames=self.propagate_renames)

        for decision in decisions:
            try:
                self._apply(decision, view, local, summary)
            except StudioMCPError:
                raise
            except Exception as exc:
                summary["errors"].append({"path": decision.studio_path, "error": _short(exc)})
                self.log.error("%s failed for %s: %s", decision.kind, decision.studio_path, _short(exc))

        place_id = self.state.place_id or self._configured_place_id()
        if place_id != self.state.place_id:
            self.state.place_id = place_id
            self.state._dirty = True
        if not self.dry_run:
            self.state.save_if_dirty()

        summary["durationMs"] = int((time.time() - started) * 1000)
        summary["changed"] = any(
            summary[k] for k in
            ("pulled", "pushed", "created", "renamed", "conflicts", "missingStudio", "missingLocal", "errors")
        )
        if summary["changed"] or summary["converged"]:
            rslog.audit(self.project.logs_dir, {"event": "cycle", **summary})
        return summary

    # ------------------------------------------------------------------
    # studio view: authoritative hashes without refetching everything
    # ------------------------------------------------------------------
    def _studio_view(self, snapshot: dict[str, dict]) -> dict[str, dict]:
        view: dict[str, dict] = {}
        to_fetch: list[str] = []

        for path, meta in snapshot.items():
            class_name = meta.get("className", "ModuleScript")
            entry = self.state.get(path)
            sha = None
            if entry.get("studioFnv") and entry["studioFnv"] == meta.get("fnv") and entry.get("sha256"):
                # Unchanged since the last sync -- reuse the known hash instead
                # of pulling ~400 KB of identical text over the bridge.
                sha = entry["sha256"]
            else:
                to_fetch.append(path)
            view[path] = {
                **meta,
                "rel": sync_engine.rel_path_for(path, class_name),
                "sha256": sha,
            }

        if to_fetch:
            sizes = {p: int(snapshot[p].get("bytes") or 0) for p in to_fetch}
            sources = studio_ops.fetch_sources(self.client, to_fetch, sizes=sizes)
            for path, source in sources.items():
                text = sync_engine.canonical(source)
                view[path]["sha256"] = sync_engine.sha256(text)
                view[path]["source"] = text

                # Refresh the change-detector cache whenever the content still
                # matches the recorded baseline.  Without this, a detector that
                # no longer agrees with the stored value -- e.g. after the hash
                # algorithm itself is corrected -- would re-fetch the script on
                # every single cycle forever.
                entry = self.state.entries.get(path)
                if entry and entry.get("sha256") == view[path]["sha256"]:
                    if entry.get("studioFnv") != snapshot[path].get("fnv"):
                        entry["studioFnv"] = snapshot[path].get("fnv")
                        self.state._dirty = True
        return view

    # ------------------------------------------------------------------
    # applying one decision
    # ------------------------------------------------------------------
    def _apply(self, decision: sync_engine.Decision, view: dict, local: dict, summary: dict) -> None:
        kind = decision.kind

        if kind in ("nothing", "converged"):
            if decision.detail.get("adopt"):
                self._adopt(decision, view, local)
            else:
                summary["converged"] += 1
            return

        if kind == "pull":
            self._do_pull(decision, view, summary)
            return

        if kind == "push":
            self._do_push(decision, view, local, summary)
            return

        if kind == "create":
            self._do_create(decision, local, summary)
            return

        if kind == "rename":
            self._do_rename(decision, summary)
            return

        if kind == "retype":
            self._do_retype(decision, summary)
            return

        if kind == "conflict":
            self._do_conflict(decision, view, summary)
            return

        if kind == "studio-missing":
            self._flag(decision, "missing-in-studio",
                       "present locally, gone from Studio -- not deleted anywhere")
            summary["missingStudio"].append(decision.studio_path)
            return

        if kind == "local-missing":
            self._flag(decision, "missing-locally",
                       "present in Studio, local file gone -- Studio copy untouched")
            summary["missingLocal"].append(decision.studio_path)
            return

        self.log.debug("unhandled decision kind %s", kind)

    # -- pull ------------------------------------------------------------
    def _do_pull(self, decision: sync_engine.Decision, view: dict, summary: dict) -> None:
        if not self.auto_pull:
            return
        meta = view.get(decision.studio_path) or {}
        text = meta.get("source")
        if text is None:
            fetched = studio_ops.fetch_sources(
                self.client, [decision.studio_path],
                sizes={decision.studio_path: int(meta.get("bytes") or 0)},
            )
            text = sync_engine.canonical(fetched.get(decision.studio_path, ""))

        expected = sync_engine.sha256(text)
        if self.dry_run:
            self.log.info("PULL (dry-run) %s -> %s", decision.studio_path, decision.rel)
            summary["pulled"].append(decision.studio_path)
            return

        self._write_local(decision.rel, text)

        # verify: read the file back and confirm the bytes actually landed
        actual = sync_engine.sha256((self.project.src_dir / decision.rel).read_text(encoding="utf-8"))
        if self.verify and actual != expected:
            raise SyncError("post-write verification failed for %s" % decision.rel)

        self.state.record(
            decision.studio_path,
            class_name=decision.class_name,
            rel=decision.rel,
            content_hash=expected,
            studio_fnv=meta.get("fnv"),
            direction="pull",
        )
        self.log.info("PULL  studio -> local  %-60s %s", decision.rel, expected[:12])
        rslog.audit(self.project.logs_dir, {
            "event": "pull", "studioPath": decision.studio_path, "rel": decision.rel,
            "sha256": expected, "bytes": len(text.encode("utf-8")), "reason": decision.reason,
        })
        summary["pulled"].append(decision.studio_path)

    # -- push ------------------------------------------------------------
    def _do_push(self, decision: sync_engine.Decision, view: dict, local: dict, summary: dict) -> None:
        if not self.auto_push or self.pull_only:
            return
        entry = local.get(decision.rel)
        if not entry:
            return

        # Only push content that has stopped changing: an editor mid-save can
        # leave a truncated file, and shipping that to Studio would be a
        # silent regression.
        if not self._is_stable(decision.rel, entry):
            self.log.debug("deferring push of %s (still settling)", decision.rel)
            return

        text = entry["text"]
        expected_local = entry["sha256"]

        # Compare-and-swap token: the Studio-side hash observed *this cycle*.
        # Taken from the live snapshot rather than from stored state, so a
        # missing/stale baseline can never degrade the write into an
        # unconditional overwrite.
        studio_fnv = (view.get(decision.studio_path) or {}).get("fnv")
        if not studio_fnv:
            self._flag(decision, "push-deferred",
                       "no studio hash available this cycle; refusing to write unguarded")
            self.log.warning("refusing unguarded push of %s (no studio hash)", decision.studio_path)
            return

        if self.dry_run:
            self.log.info("PUSH (dry-run) %s -> %s", decision.rel, decision.studio_path)
            summary["pushed"].append(decision.studio_path)
            return

        result = studio_ops.write_sources(self.client, [{
            "path": decision.studio_path,
            "source": text,
            "expected_fnv": studio_fnv,
        }]).get(decision.studio_path, {})

        status = result.get("status")
        if status == "written":
            new_fnv = result.get("newFnv")
            if self.verify and not self._verify_studio(decision.studio_path, expected_local):
                self._flag(decision, "verify-failed",
                           "wrote to Studio but read-back did not match; leaving baseline untouched")
                summary["errors"].append({"path": decision.studio_path, "error": "verify failed"})
                return
            self.state.record(
                decision.studio_path,
                class_name=decision.class_name,
                rel=decision.rel,
                content_hash=expected_local,
                studio_fnv=new_fnv,
                direction="push",
            )
            self.log.info("PUSH  local -> studio  %-60s %s", decision.rel, expected_local[:12])
            rslog.audit(self.project.logs_dir, {
                "event": "push", "studioPath": decision.studio_path, "rel": decision.rel,
                "sha256": expected_local, "bytes": len(text.encode("utf-8")), "fnv": new_fnv,
            })
            summary["pushed"].append(decision.studio_path)
            return

        if status == "conflict":
            self._flag(decision, "conflict",
                       "Studio changed since it was read; local edit was NOT written")
            summary["conflicts"].append(decision.studio_path)
            return

        self._flag(decision, "push-failed", "studio write returned %r: %s"
                   % (status, result.get("detail")))
        summary["errors"].append({"path": decision.studio_path, "error": str(status)})

    # -- create ----------------------------------------------------------
    def _do_create(self, decision: sync_engine.Decision, local: dict, summary: dict) -> None:
        if not self.auto_push or self.pull_only:
            return
        entry = local.get(decision.rel)
        if not entry:
            return
        if not self._is_stable(decision.rel, entry):
            return

        parent = ".".join(decision.studio_path.split(".")[:-1])
        name = decision.studio_path.split(".")[-1]

        if self.dry_run:
            self.log.info("CREATE (dry-run) %s", decision.studio_path)
            summary["created"].append(decision.studio_path)
            return

        result = studio_ops.create_script(
            self.client, parent, name, decision.class_name, entry["text"]
        )
        if not result.get("ok"):
            # "exists" means a duplicate would have been created -- refuse and
            # let the next cycle treat it as a normal push instead.
            self.log.warning("create skipped for %s: %s", decision.studio_path, result.get("status"))
            summary["errors"].append({"path": decision.studio_path, "error": str(result.get("status"))})
            return

        fetched = studio_ops.fetch_sources(self.client, [decision.studio_path])
        actual = sync_engine.sha256(sync_engine.canonical(fetched.get(decision.studio_path, "")))
        if self.verify and actual != entry["sha256"]:
            self._flag(decision, "verify-failed", "created in Studio but content differs")
            summary["errors"].append({"path": decision.studio_path, "error": "verify failed"})
            return

        self.state.record(
            decision.studio_path,
            class_name=decision.class_name,
            rel=decision.rel,
            content_hash=entry["sha256"],
            studio_fnv=result.get("fnv"),
            direction="create",
        )
        self.log.info("CREATE local -> studio %-59s %s", decision.rel, entry["sha256"][:12])
        rslog.audit(self.project.logs_dir, {
            "event": "create", "studioPath": decision.studio_path, "rel": decision.rel,
            "sha256": entry["sha256"], "className": decision.class_name,
        })
        summary["created"].append(decision.studio_path)

    # -- rename ----------------------------------------------------------
    def _do_rename(self, decision: sync_engine.Decision, summary: dict) -> None:
        if not self.auto_pull:
            return
        old_rel = decision.detail["fromRel"]
        new_rel = decision.detail["toRel"]
        source = self.project.src_dir / old_rel
        if not source.is_file():
            return

        if self.dry_run:
            self.log.info("RENAME (dry-run) %s -> %s", old_rel, new_rel)
            summary["renamed"].append({"from": old_rel, "to": new_rel})
            return

        # Preserve the file under backups/renames before moving it, so a
        # mistaken rename in Studio never loses the local copy.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        keep = self.project.backups_dir / "renames" / stamp / old_rel
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_bytes(source.read_bytes())

        target = self.project.src_dir / new_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)

        text = sync_engine.canonical(target.read_text(encoding="utf-8"))
        self.state.forget(decision.detail["fromStudio"])
        self.state.record(
            decision.studio_path,
            class_name=decision.class_name,
            rel=new_rel,
            content_hash=sync_engine.sha256(text),
            studio_fnv=decision.detail.get("studioFnv"),
            direction="rename",
        )
        self.log.info("RENAME %s -> %s (previous copy kept in backups/renames/%s)",
                      old_rel, new_rel, stamp)
        rslog.audit(self.project.logs_dir, {
            "event": "rename", "from": old_rel, "to": new_rel,
            "fromStudio": decision.detail.get("fromStudio"),
            "toStudio": decision.studio_path, "preserved": str(keep),
        })
        summary["renamed"].append({"from": old_rel, "to": new_rel})

    # -- retype ----------------------------------------------------------
    def _do_retype(self, decision: sync_engine.Decision, summary: dict) -> None:
        """A Studio script changed class, so its filename suffix must change.

        The content baseline is deliberately preserved and the change-detector
        hash is cleared.  Clearing it forces the next cycle to re-read the
        source and work out whether content changed too, instead of trusting a
        cached hash that belonged to the previous class.
        """
        old_rel = decision.detail["fromRel"]
        new_rel = decision.detail["toRel"]
        source = self.project.src_dir / old_rel
        baseline = (self.state.get(decision.studio_path) or {}).get("sha256")

        if self.dry_run:
            self.log.info("RETYPE (dry-run) %s: %s -> %s", decision.studio_path, old_rel, new_rel)
            summary["renamed"].append({"from": old_rel, "to": new_rel})
            return

        if source.is_file():
            target = self.project.src_dir / new_rel
            if target.exists():
                # Rename detection requires the destination to be locally
                # absent, so this only fires if a file appeared in between.
                # Refuse rather than clobber it.
                self._flag(decision, "rename-blocked",
                           "destination %s already exists; nothing was moved" % new_rel)
                self.log.warning("rename of %s blocked: %s already exists", old_rel, new_rel)
                summary["errors"].append({"path": decision.studio_path,
                                          "error": "rename destination exists"})
                return
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            keep = self.project.backups_dir / "renames" / stamp / old_rel
            keep.parent.mkdir(parents=True, exist_ok=True)
            keep.write_bytes(source.read_bytes())
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)

        self.state.record(
            decision.studio_path,
            class_name=decision.class_name,
            rel=new_rel,
            content_hash=baseline,
            studio_fnv=None,          # force a fresh read to revalidate content
            direction="retype",
        )
        self.log.info("RETYPE %s: %s -> %s (%s -> %s)",
                      decision.studio_path, old_rel, new_rel,
                      decision.detail.get("fromClass"), decision.detail.get("toClass"))
        rslog.audit(self.project.logs_dir, {
            "event": "retype", "studioPath": decision.studio_path,
            "from": old_rel, "to": new_rel,
            "fromClass": decision.detail.get("fromClass"),
            "toClass": decision.detail.get("toClass"),
        })
        summary["renamed"].append({"from": old_rel, "to": new_rel})

    # -- conflict --------------------------------------------------------
    def _do_conflict(self, decision: sync_engine.Decision, view: dict, summary: dict) -> None:
        """Overwrite nothing.  Park the Studio version beside the local file.

        Must be idempotent.  A conflict is re-detected on every poll until the
        user resolves it (the baseline deliberately stays put), so writing a
        fresh timestamped copy each cycle would grow without bound -- one file
        every couple of seconds, forever, in a OneDrive-synced folder.  The
        conflict signature below makes repeats a no-op and caps this script at
        a single parked copy.
        """
        signature = "%s:%s" % (
            decision.detail.get("studioSha"), decision.detail.get("localSha"))
        target = self.project.src_dir / decision.rel
        existing = sorted(target.parent.glob(target.stem + ".conflict-*.luau"))
        already_parked = (
            self.state.get(decision.studio_path).get("conflictSignature") == signature
            and bool(existing)
        )

        if already_parked:
            # Same unresolved conflict as the last cycle: nothing to write.
            summary["conflicts"].append(decision.studio_path)
            summary["conflictRepeats"] = summary.get("conflictRepeats", 0) + 1
            self.log.debug("conflict on %s still unresolved; reusing %s",
                           decision.studio_path, existing[0].name)
            return

        meta = view.get(decision.studio_path) or {}
        text = meta.get("source")
        if text is None:
            try:
                fetched = studio_ops.fetch_sources(
                    self.client, [decision.studio_path],
                    sizes={decision.studio_path: int(meta.get("bytes") or 0)},
                )
                text = sync_engine.canonical(fetched.get(decision.studio_path, ""))
            except StudioMCPError as exc:
                # Writing a "conflict copy" that contains only a header would
                # be a misleading artifact: it would look like the Studio
                # version had been saved when in fact the read failed.  Flag
                # it instead and leave the disk alone.
                self.log.error("conflict on %s but the Studio copy could not be read (%s); "
                               "no conflict file written", decision.studio_path, _short(exc))
                self._flag(decision, "conflict-unreadable",
                           "both sides changed but the Studio copy could not be read: %s"
                           % _short(exc), extra={"conflictSignature": signature})
                summary["conflicts"].append(decision.studio_path)
                return

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        conflict_path = target.with_name("%s.conflict-%s.luau" % (target.stem, stamp))

        if not self.dry_run:
            # A newer conflict supersedes the older parked copies for this
            # script: the freshest one describes current Studio content, so
            # keeping the older ones would only accumulate stale duplicates.
            for stale in existing:
                try:
                    stale.unlink()
                except OSError:
                    pass
            conflict_path.parent.mkdir(parents=True, exist_ok=True)
            conflict_path.write_text(
                "-- RobloxStudioLive conflict copy -- STUDIO version\n"
                "-- Detected %s\n"
                "-- The local file was left untouched; nothing was overwritten.\n"
                "-- Resolve with:  rss resolve --path \"%s\" --take studio|local\n"
                % (stamp, decision.studio_path)
                + text,
                encoding="utf-8", newline="",
            )
        self._flag(decision, "conflict",
                   "both sides changed; studio copy saved as %s" % conflict_path.name,
                   extra={"conflictSignature": signature,
                          "conflictFile": conflict_path.name})
        self.log.warning("CONFLICT %s -- local kept, Studio copy -> %s",
                         decision.studio_path, conflict_path.name)
        rslog.audit(self.project.logs_dir, {
            "event": "conflict", "studioPath": decision.studio_path, "rel": decision.rel,
            "conflictFile": str(conflict_path),
            "studioSha": decision.detail.get("studioSha"),
            "localSha": decision.detail.get("localSha"),
            "baseline": decision.detail.get("baseline"),
            "supersededCopies": len(existing),
        })
        summary["conflicts"].append(decision.studio_path)

    # -- helpers ---------------------------------------------------------
    def _adopt(self, decision: sync_engine.Decision, view: dict, local: dict) -> None:
        """Both sides already identical -- just refresh the baseline."""
        meta = view.get(decision.studio_path) or {}
        entry = local.get(decision.rel) or {}
        self.state.record(
            decision.studio_path,
            class_name=decision.class_name,
            rel=decision.rel,
            content_hash=entry.get("sha256") or meta.get("sha256"),
            studio_fnv=meta.get("fnv"),
            direction="adopt",
        )

    def _verify_studio(self, studio_path: str, expected_sha: str) -> bool:
        """Independent read-back: fetch the source again and SHA-256 it.

        Deliberately a *separate* round trip from the write, and hashed with the
        authoritative digest rather than reusing the FNV the write reported --
        otherwise verification would only be confirming its own echo.
        """
        fetched = studio_ops.fetch_sources(self.client, [studio_path])
        if studio_path not in fetched:
            return False
        return sync_engine.sha256(fetched[studio_path]) == expected_sha

    def _is_stable(self, rel: str, entry: dict) -> bool:
        """True once the file has stopped changing.

        Age is measured from the file's mtime rather than a per-process cycle
        counter, so the guard also works for a one-shot `rss sync` (where an
        in-memory counter would always read "first cycle" and silently never
        push anything).  A file being actively written keeps a fresh mtime and
        stays deferred, which is exactly what we want.
        """
        age = time.time() - float(entry.get("mtime") or 0.0)
        return age >= self.settle_seconds

    def _write_local(self, rel: str, text: str) -> None:
        """Atomic write: a reader never observes a partially written file."""
        target = self.project.src_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".rss-tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp, target)

    def _flag(self, decision: sync_engine.Decision, status: str, detail: str,
              extra: dict | None = None) -> None:
        if self.dry_run:
            return
        self.state.mark(decision.studio_path, status, detail, extra=extra)
        self.state.save_if_dirty()

    def _configured_place_id(self) -> int | None:
        try:
            return int((self.project.settings.get("place") or {}).get("placeId"))
        except (TypeError, ValueError):
            return None

    def _safely_reconnect(self) -> None:
        try:
            self.client.restart()
            self.log.info("reconnected to studio")
        except Exception as exc:
            self.log.warning("reconnect failed: %s", _short(exc))

    def _sleep(self, seconds: float) -> None:
        deadline = time.time() + max(0.05, seconds)
        while not self._stop and time.time() < deadline:
            time.sleep(min(0.25, deadline - time.time()))


def _fmt(summary: dict) -> str:
    return "pulled=%d pushed=%d created=%d renamed=%d conflicts=%d missingStudio=%d missingLocal=%d errors=%d in %dms" % (
        len(summary["pulled"]), len(summary["pushed"]), len(summary["created"]),
        len(summary["renamed"]), len(summary["conflicts"]), len(summary["missingStudio"]),
        len(summary["missingLocal"]), len(summary["errors"]), summary["durationMs"],
    )


def _short(exc: BaseException) -> str:
    text = str(exc).strip().replace("\n", " ")
    return text[:200]
