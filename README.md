# RobloxStudioLive

Real-time, bidirectional synchronization between **Roblox Studio** and a folder
on your PC, driven by the Roblox Studio **MCP** server.

Edit a script in Studio and the local file updates by itself. Edit a local file
and Studio updates by itself. Ask an AI agent for a change and it lands in both.

* Studio ↔ local sync: **working and verified** (see [Verified results](#verified-results))
* Your existing game: **unchanged** — nothing is deleted, renamed or replaced
* Deletions are **never** propagated, in either direction
* Every write is verified by reading it back before it is recorded as synced
* Your game, your settings and your logs are **never committed** — see
  [What is never published](#what-is-never-published)

---

## Requirements

| | |
| --- | --- |
| OS | **Windows 10 or 11** |
| Roblox Studio | installed, with the place you want to mirror open in **Edit** mode |
| Python | **3.9 or newer** — [python.org](https://www.python.org/downloads/), or `winget install -e --id Python.Python.3.12` |
| Git | optional — only if you want the mirror under version control |

Windows only, because the tool drives Studio through the `StudioMCP.exe` binary
that Roblox ships inside its Windows Studio install. No Roblox API key is
involved, nothing is installed into your place, and no project setting is
changed.

---

## Install

```bat
:: 1. get the code
git clone https://github.com/<you>/RobloxStudioLive.git
cd RobloxStudioLive

:: 2. one-time setup - double-click init.cmd, or run it:
init.cmd
```

`init.cmd` finds a Python interpreter, proves the checkout works, and creates
everything a fresh clone is missing:

| It creates | From |
| --- | --- |
| `config\settings.json` | `config\settings.example.json` — which services to mirror |
| `config\python-path.txt` | the interpreter it validated on this machine |
| `default.project.json` | your mirror roots (the optional Rojo project file) |
| `src\` `backups\` `logs\` | empty, ready for the daemon |

It is **idempotent** and **non-destructive**: run it as often as you like. An
existing `config\settings.json` is never overwritten — pass `--force` to rebuild
it from the template, which keeps your previous version as
`config\settings.json.bak`.

```bat
init.cmd --check              :: report only, change nothing
init.cmd --studio             :: also test the live connection to Studio
init.cmd --no-tests           :: skip the offline self-test
init.cmd --roots ServerScriptService,ReplicatedStorage
init.cmd --help               :: every option
```

A successful run looks like this:

```
  RobloxStudioLive - setup
  =======================

  python   : 3.12.10   (py -3)
  project  : C:\dev\RobloxStudioLive

  [ ok ] folders        created src, backups, logs, config, docs
  [ ok ] settings       created config\settings.json from the template
  [ ok ] rojo project   created default.project.json for 7 services
  [ ok ] python path    config\python-path.txt -> C:\Python312\python.exe
  [ ok ] checkout       8 tool modules import cleanly
  [ ok ] studio binary  C:\...\Roblox\Versions\version-xxxx\StudioMCP.exe
  [ ok ] self-test      tests\test_sync_engine.py: 45/45 assertions pass
```

`studio binary` is a warning rather than a failure when Studio is not installed
yet — you can set the project up first and install Studio afterwards. If Python
is missing, `init.cmd` says so and tells you how to get it, instead of failing
deeper in with a stack trace.

---

## Quick start

```bat
:: 1. open Roblox Studio with the place you want to mirror, in Edit mode

:: 2. pull your existing scripts down - this reads Studio, writes nothing
rss.cmd import

:: 3. start the live sync daemon
start-sync.cmd

:: 4. in another window, check on it
rss.cmd status
rss.cmd conflicts
```

Stop it with `stop-sync.cmd`, or Ctrl+C in the daemon window.

> `init.cmd`, `start-sync.cmd` and `rss.cmd` each locate a Python interpreter
> themselves, so `python` does not have to be on your `PATH` — which matters on
> Windows, where the first `python.exe` on `PATH` is often the Microsoft Store
> placeholder. When you do have a real interpreter on `PATH`,
> `python tools\rss.py status` is exactly equivalent to `rss.cmd status`.

---

## What is in here

```
RobloxStudioLive/
├── init.cmd                  one-time setup - start here
├── start-sync.cmd            start the live daemon
├── stop-sync.cmd             stop it
├── rss.cmd                   the CLI, without needing Python on PATH
├── run-daemon.cmd            headless daemon run (what the logon task uses)
├── install-autostart.cmd     OPTIONAL: start at logon
├── uninstall-autostart.cmd   undo the above
├── src/                      your game's scripts, mirroring Studio   [ignored]
├── backups/                  timestamped snapshots of both sides     [ignored]
│   └── renames/              previous path kept when a rename is applied
├── logs/                                                            [ignored]
│   ├── sync-YYYY-MM-DD.log   human-readable sync log
│   └── audit.jsonl           append-only machine-readable record
├── config/
│   ├── settings.example.json template - committed
│   ├── settings.json         yours: roots, intervals, placeId       [ignored]
│   ├── structure.json        instance-tree snapshot (`rss tree`)    [ignored]
│   └── python-path.txt       interpreter validated by init.cmd      [ignored]
├── tools/                    the implementation
├── tests/                    test suites (see below)
├── default.project.json      optional Rojo project file, matches src/
├── sync-state.json           the baseline used for 3-way diffing    [ignored]
└── README.md
```

`[ignored]` means the path is in `.gitignore` and never leaves your machine.

### File naming preserves the script class

| Studio class   | Local file        |
| -------------- | ----------------- |
| `Script`       | `Foo.server.luau` |
| `LocalScript`  | `Foo.client.luau` |
| `ModuleScript` | `Foo.luau`        |

This is Rojo's convention, so `default.project.json` also works if you ever want
Rojo for building — but Rojo alone does **not** provide bidirectional sync, which
is what this project adds.

Nested instances keep their depth: a `LocalScript` named `TowerMovement` inside
`StarterPlayer.StarterCharacterScripts` becomes
`src/StarterPlayer/StarterCharacterScripts/TowerMovement.client.luau`.

---

## Commands

`rss.cmd <command>` and `python tools\rss.py <command>` are interchangeable.

| Command | What it does |
| --- | --- |
| `rss.cmd watch` | run the live daemon (default command) |
| `rss.cmd status` | connection, counts, unresolved items |
| `rss.cmd import` | first-time import, Studio → local only |
| `rss.cmd sync` | exactly one cycle, then exit |
| `rss.cmd sync --dry-run` | show what *would* happen |
| `rss.cmd verify` | prove both sides are byte-identical |
| `rss.cmd conflicts` | list anything needing a decision |
| `rss.cmd resolve --path "ServerScriptService.Foo" --take studio\|local` | resolve one item explicitly |
| `rss.cmd diff --path "ServerScriptService.Foo"` | unified diff of one script |
| `rss.cmd tree` | dump the Studio instance tree |
| `rss.cmd backup [label]` | timestamped backup of both sides |

Add `--json` to any command for machine-readable output.

---

## Safety model

These are enforced in code, not by convention.

**Deletions never propagate.** Deleting a script in Studio leaves the local file
alone and flags it `missing-in-studio`. Deleting a local file leaves the Studio
script alone and flags it `missing-locally`. Nothing is ever removed on the
other side automatically.

**Conflicts overwrite nothing.** If both sides changed since the last sync, the
Studio version is written beside your file as
`Foo.conflict-<timestamp>.luau`, your local file is left exactly as it was, and
the item is marked unresolved. You choose with `rss resolve`.

This is **bounded to one parked copy per script**. An unresolved conflict is
re-detected on every poll by design, so without care it would mint a new file
every couple of seconds forever. A `conflictSignature` makes repeats a no-op and
lets a newer Studio edit supersede the older copy. See
`tests/test_conflict_bounded.py`.

**Writes are compare-and-swap.** Every Studio write carries the hash of what was
last read. If Studio changed in between, the write is *refused* and becomes a
conflict rather than clobbering the newer edit.

**Every write is verified.** After a push the script is re-read from Studio and
rehashed; after a pull the file is re-read from disk and rehashed. A write is
only recorded as synced once the read-back matches.

**No half-written files.** A local file must be unchanged for `settleSeconds`
before it is pushed, so an editor caught mid-save never ships a truncated script
to Studio.

**Loop prevention.** Each script stores the content hash agreed at the last sync
(`sync-state.json` → `entries[*].sha256`). A cycle compares *studio now*,
*local now* and *that baseline*, so a change this tool just made is recognised
as already-known instead of as a fresh edit. Repeated cycles converge to zero
actions.

**Renames are moves, not delete+create.** If a script vanishes from one path and
appears at another with identical content, and the match is unambiguous, the
local file is moved. The previous path is preserved under `backups/renames/`.
If the match is ambiguous, nothing is moved.

**Unrelated files are ignored.** Only `*.luau` under `src/` is considered.
Conflict copies, `*.bak`, `*.tmp` and dot-directories are skipped. Files outside
`src/` are never read or written.

**No global state is touched.** This project does not modify your Rojo config,
your Studio plugins folder, or any place setting. `HttpService.HttpEnabled`
stays `false` — network access is not needed.

---

## How it works

Roblox Studio's MCP server is request/response only: it exposes tools but has no
push notification for "a script changed". So the daemon closes that gap with a
poll-and-diff loop:

```
   ┌──────────────┐   1. snapshot (names + cheap hash)    ┌──────────────────┐
   │    Studio    │ ◄──────────────────────────────────── │                  │
   │  (Edit mode) │   2. fetch source, only if changed     │  rss watch       │
   │              │ ─────────────────────────────────────► │  (this project)  │
   └──────────────┘   3. compare-and-swap write            └────────┬─────────┘
                             + read-back verify                     │
                                                         4. 3-way diff vs
                                                            sync-state.json
                                                                   │
                                                         5. atomic file write
                                                                   ▼
                                                          ┌──────────────────┐
                                                          │  src/**/*.luau   │
                                                          └──────────────────┘
```

The connection is a real MCP session: the daemon spawns Roblox's own
`StudioMCP.exe` (from `%LOCALAPPDATA%\Roblox\Versions\<ver>\`) and speaks
JSON-RPC 2.0 over stdio. It keeps that session open across polls, and restarts
it automatically if it dies.

A poll costs **~75 ms for 66 scripts**: the snapshot returns only names, classes
and a 32-bit FNV-1a change-detector hash. Full source is fetched only for scripts
whose hash actually moved. The authoritative digest is SHA-256, computed on the
Python side from the fetched text.

See `docs/ARCHITECTURE.md` for the transport limits discovered while building
this (response truncation at 100 000 chars, the `[Complex Table]` substitution,
Luau literal escaping, and the UTF-8-safe slicing that works around them).

---

## Configuration

`config/settings.json` — created by `init.cmd`, never committed:

```jsonc
{
  "roots": ["ServerScriptService", "ReplicatedStorage", "StarterGui",
            "StarterPlayer", "ReplicatedFirst", "ServerStorage", "Workspace"],
  "sync": {
    "pollIntervalSeconds": 2.0,   // how often Studio is polled
    "settleSeconds": 1.5,         // a file must be quiet this long before pushing
    "autoPull": true,             // Studio -> local
    "autoPush": true,             // local -> Studio
    "propagateRenames": true      // move local files when Studio renames a script
  }
}
```

`roots` chooses which services are mirrored; a service you leave out is never
read and never written. To change it, edit the file and re-run
`init.cmd --force` (or `init.cmd --force --roots A,B,C`) so that
`default.project.json` follows. If you have service folders in `ServerStorage`
that you would rather not mirror — snapshot or backup folders, for instance —
just drop `"ServerStorage"` from `roots`.

The optional `place` block (`{"placeId": …, "experienceName": …}`) is
informational only: it is recorded into `sync-state.json` as a guard against
pointing one project at two different places. `rss status` reads the live place
from Studio regardless.

Deliberately **absent**: there is no `propagateDeletions` or `conflictPolicy`
switch. The daemon has no delete path at all and conflicts always park a copy,
so there is nothing to configure and nothing to get wrong. Advertising a switch
that no code reads would be worse than having none.

Only one process may sync a project at a time. `rss sync` and `rss watch` take
an exclusive lock, so a second one exits with a clear message instead of racing.
Read-only commands (`status`, `conflicts`, `verify`, `diff`, `tree`) never lock.

### Running it automatically at logon (optional)

`install-autostart.cmd` creates one Scheduled Task named "RobloxStudioLive Sync"
that runs `run-daemon.cmd` at logon; `uninstall-autostart.cmd` removes it.
Neither is run for you — both modify system state, so they are left for you to
invoke deliberately. The daemon's output goes to `logs/daemon.log`.

---

## What is never published

The repository is the **tool**. The mirror of your place is not part of it, and
`.gitignore` enforces that. Before you push anything, `git status --short` is
the check that matters.

| Path | Why it is ignored |
| --- | --- |
| `src/` | your game's scripts, mirrored out of Studio |
| `backups/` | full copies of both sides of your place, plus rename history |
| `logs/` | every line carries an absolute path, and therefore your username |
| `sync-state.json` | the diff baseline: every script path in your place, plus your placeId |
| `config/settings.json` | your roots, intervals and placeId |
| `config/structure.json` | a dump of your place's instance tree |
| `config/python-path.txt` | the interpreter path on this machine |
| `.rss-daemon.lock` | pid of the running daemon |
| `__pycache__/`, `*.pyc` | bytecode |
| `*.conflict-*.luau`, `*.rss-tmp`, `*.bak`, `*.tmp` | scratch files, including parked conflict copies |
| `.env`, `*.key`, `*.pem`, `credentials.json`, `secrets.json` | defensive: this project needs no credential, so a stray one should never be committable |

A fresh clone contains none of these, which is exactly why `init.cmd` exists: it
recreates the four files that are needed to run, from the committed template.

---

## Verified results

Everything below was executed against a real place during development — **66
scripts, 1 484 255 bytes** — and is reproducible on any place with the suites in
`tests/`.

### Test suites

| Suite | Command | Result |
| --- | --- | --- |
| Offline decision logic | `python tests\test_sync_engine.py` | **45 / 45 pass** |
| Studio bridge + codegen | `python tests\selftest_studio_ops.py` | **34 / 34 pass** |
| Codegen edges + exact FNV-1a | `python tests\test_codegen_edges.py` | **25 / 25 pass** |
| Conflict copies stay bounded | `python tests\test_conflict_bounded.py` | **12 / 12 pass** |
| Live bidirectional sync | `python tests\acceptance_auto.py` (daemon running) | **8 / 8 pass** |
| Safety: conflicts, deletions, renames | `python tests\acceptance_safety.py` (daemon stopped) | **35 / 35 pass** |
| Sustained operation + restart | `python tests\acceptance_soak.py` | **17 / 17 pass** |
| Interruption / reconnect | `python tests\acceptance_reconnect.py` | **10 / 10 pass** |

186 assertions, all passing. `python tests\run_all.py` runs the four suites that
need no coordination; `--live` adds the rest.

The only suite that needs no Studio at all is `test_sync_engine.py`, which is why
that is the one `init.cmd` runs. `test_codegen_edges.py` and
`test_conflict_bounded.py` talk to Studio and will make and remove a scratch
script in your place, so they are not run for you.

`acceptance_auto.py` needs the daemon running and `acceptance_safety.py` needs it
stopped, so run the live suites **sequentially** — running two at once against
one Studio place makes them see each other's scratch scripts as churn.

### MVP acceptance criteria

| # | Criterion | Status | Evidence |
| --- | --- | --- | --- |
| 1 | MCP connects successfully | ✅ | 28 tools, `/health` shows 1 studio; 17 ms round trip |
| 2 | Existing game scripts import to Desktop | ✅ | 66 scripts / 1 484 255 bytes, byte-identical |
| 3 | Editing a script in Studio updates the local file automatically | ✅ | detected in **0.8 s**, no manual step |
| 4 | Editing a local script updates the correct Studio script automatically | ✅ | detected in **4.3 s**, no manual step |
| 5 | An AI prompt can create or modify a Studio script | ✅ | local file auto-created in Studio, byte-exact |
| 6 | Repeated sync does not create duplicates | ✅ | 3 passes, 0 actions, still 66 scripts |
| 7 | Disconnect/reconnect does not lose changes | ✅ | bridge killed; 66 files intact, 0 churn, auto-recovered |
| 8 | Conflicting edits do not silently destroy code | ✅ | both sides preserved, conflict copy written, flagged |
| 9 | Unrelated projects and files remain untouched | ✅ | sentinels inside and outside the project unchanged |

The place used for development was left exactly as found: **66 scripts before,
66 after**, and `rss verify` reported **66/66 byte-identical**.

---

## Troubleshooting

**"Could not find a Python interpreter"** — install Python 3.9+ and run
`init.cmd` again, or point the launchers straight at one:

```bat
set RSS_PYTHON=C:\path\to\python.exe
start-sync.cmd
```

**"This project has not been set up yet"** — `config\settings.json` is missing.
Run `init.cmd`.

**"a daemon is already running (pid N)"** — one daemon per project. Stop the
other one with `stop-sync.cmd`, or wait for it to exit.

**"no Roblox Studio instance is attached"** — Studio is closed or still
starting. The daemon keeps retrying with backoff; local files are untouched.
Check with `rss.cmd status`. `init.cmd --studio` tests the same link once.

**A script shows as `missing-in-studio`** — it was deleted in Studio. Your local
file was kept. Re-create it in Studio and the next cycle will re-link, or delete
the local file yourself if the removal was intentional.

**Something looks wrong** — `rss.cmd conflicts` lists anything unresolved,
`logs/audit.jsonl` has a machine-readable record of every change, and `backups/`
holds full snapshots. Restore by copying files back; nothing here is one-way.

---

## License

MIT — see [LICENSE](LICENSE).
