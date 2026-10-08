# Architecture

Notes on how RobloxStudioLive is put together, and — more usefully — the
non-obvious transport limits that were discovered by experiment while building
it. Every item under "Bridge limits" was found the hard way and is now handled
in code with a test that fails if the handling regresses.

---

## 1. The connection

Roblox Studio ships an MCP server binary at:

```
%LOCALAPPDATA%\Roblox\Versions\<version>\StudioMCP.exe
```

It is normally spawned as a stdio child by an MCP client. There is nothing
stopping a second client from doing the same, so the daemon spawns its own
instance and speaks **JSON-RPC 2.0, newline-delimited, over stdin/stdout**:

```python
client = StudioMCPClient()          # spawns StudioMCP.exe
client.list_tools()                 # 28 tools
client.execute_luau("return {ok = true}")
```

A broker process also listens on `http://127.0.0.1:13469/health` and reports
`Studios: N`, `Proxies: N`, `Tools cached: N`. That is used for diagnostics in
`rss status`; the actual work goes over stdio.

**Why not a Studio plugin?** A plugin would need `HttpService` to reach a local
bridge, which means enabling HTTP requests in the user's place — a persisted
change to their game settings. Spawning the official binary requires no change
to the place at all. `HttpService.HttpEnabled` is still `false`.

### Session lifetime

One `StudioMCPClient` stays open for the whole daemon run. A reader thread
demultiplexes responses to waiting callers by id.

> **Deadlock worth remembering:** an early version held one `RLock` for the whole
> of `start()`, including the `initialize` round trip. The reader thread needed
> that same lock to look up the pending request, so every call blocked until
> timeout. Lifecycle locking (`_lifecycle_lock`) and response routing (`_lock`)
> are now separate, and `_lock` is never held across a round trip.

Reconnection: `_request_with_reconnect` restarts the child once and retries. A
killed bridge recovers transparently, including mid-cycle
(`tests/acceptance_reconnect.py`).

---

## 2. Bridge limits (all discovered experimentally)

These are properties of the Studio `execute_luau` tool, not of MCP itself.

| Limit | Observed behaviour | How it is handled |
| --- | --- | --- |
| **Response truncation** | A tool response is cut at **~100 000 characters** | Reads budget to 76 000 chars, slice large scripts, and re-parse strictly |
| **Complex-table substitution** | A returned Luau table above **~90 KB** becomes the literal string `"[Complex Table]"` | Return `HttpService:JSONEncode(...)` (a plain string) instead of a table |
| **Array re-encoding** | A returned Luau array arrives as `{"1": ..., "2": ...}`; an empty table as `[]` | Return **path-keyed maps**, never positional arrays |
| **Input is not capped** | 400 000 chars of code executed fine | Write batching is for latency, not correctness |
| **200-local ceiling** | >200 locals in one chunk is a parse error | Generated Luau uses few locals |
| **No `HttpService:Base64Encode`** | Does not exist in Roblox | Not used; text is transferred as JSON strings |
| **Lua lexer folds CR in long strings** | `\r`, `\n`, `\r\n` all become `\n` inside `[[...]]` | Text containing `\r` is emitted as segments joined by an explicit `"\r"` escape |

### Two bugs that only a test would catch

**JSON is not Lua.** Embedding a JSON array directly into Luau produces
`local ROOTS = ["ServerScriptService"]`, where `[...]` is an *index expression*,
not a table constructor — a parse error. `lua_value()` emits real Luau literals.

**`[` + `[[...]]` lexes as `[[[`.** Indexing a table with a long-string key
without a separator is ambiguous. Non-identifier keys are emitted as quoted
literals: `["weird key"] = ...`.

**Percent-formatting corrupts generated code.** Script bodies contain `%`
(`string.format("%d%%")`), `{}` and backslashes. Luau is therefore assembled by
single-pass `@@TOKEN@@` substitution, never `str.format` or `%`.

### Byte-exact transfer

Source is transferred as slices, each cut on a UTF-8 character boundary (walk
back off continuation bytes `0x80–0xBF`) so a multi-byte character can never be
split across a JSON round trip and replaced with U+FFFD. Slices are reassembled
and checked against the length Studio reported; a mismatch raises instead of
returning partial text, because partial text would later be written back over
good code.

`_plan_fetch` spreads slices of one script across *separate* calls — packing a
140 KB script's three slices into one response is exactly the mistake that the
truncation guard exists to catch.

---

## 3. Change detection

A poll must be cheap; a full refetch of the corpus is 1.4 MB.

1. **Snapshot** (~75 ms for 66 scripts): name, class, parent path, byte count and
   a 32-bit **FNV-1a** hash computed in Luau. No source crosses the wire.
2. **Fetch**: only scripts whose FNV differs from the one recorded at the last
   sync. The authoritative digest is **SHA-256**, computed in Python over
   line-ending-normalised text.
3. **Cache**: an unchanged script reuses the stored SHA-256, so a steady-state
   cycle transfers no source at all.

FNV-1a here is only a change *detector*; a collision is harmless because Python
rehashes whatever it fetches.

### Canonical form

`sync_engine.canonical()` normalises `\r\n` and `\r` to `\n` before hashing or
writing. Roblox stores LF and Windows editors can introduce CRLF; hashing one
canonical form on both sides prevents an endless edit/echo loop caused purely by
line endings.

---

## 4. Decisions (the part that protects your code)

`sync_engine.plan()` is pure: three plain dicts in, a list of decisions out. No
MCP, no disk, no clock — which is why the rules can be tested exhaustively
without Studio running (`tests/test_sync_engine.py`, 45 cases).

With `S` = studio hash now, `L` = local hash now, `B` = hash at last sync:

| Situation | Action |
| --- | --- |
| `S` absent, `L` absent | nothing |
| `S` present, `L` absent, `B` absent | `pull` — new script in Studio |
| `S` present, `L` absent, `B` present | `local-missing` — flag, **never delete in Studio** |
| `S` absent, `L` present, `B` absent | `create` — new local file goes to Studio |
| `S` absent, `L` present, `B` present | `studio-missing`, or `rename` if matched |
| `S == L` | `converged` — refresh baseline, no I/O |
| `S != L`, `S == B` | `push` — local edited |
| `S != L`, `L == B` | `pull` — Studio edited |
| `S != L`, `S != B`, `L != B` | `conflict` — overwrite nothing |

`B` is what makes loop prevention work: after a push, the baseline becomes the
pushed hash, so the next cycle sees `S == L == B` and does nothing.

### Conflict copies are idempotent and bounded

A conflict stays detected on every poll until the user resolves it — the
baseline deliberately does not move, because moving it would mean silently
accepting one side. That makes `_do_conflict` a hot path, and the first version
of it was wrong: it minted a fresh `.conflict-<stamp>.luau` every cycle, so an
unresolved conflict produced one new file every couple of seconds, forever, in a
OneDrive-synced folder.

Each entry now stores a `conflictSignature` of `studioSha:localSha`:

* signature unchanged and a copy exists → nothing is written, logged at WARNING,
  or saved to disk;
* signature changed → the superseded copy for that path is removed and exactly
  one new copy is written.

So a script can have at most one parked conflict copy at a time.
`tests/test_conflict_bounded.py` runs eight cycles against an unresolved
conflict and asserts the count stays at 1, then edits Studio again and asserts
the copy is *refreshed* rather than accumulating. This was a real defect found
by an independent verifier, not a hypothetical.

State writes are likewise dirty-tracked (`save_if_dirty`): an idle cycle no
longer rewrites a 30 KB `sync-state.json` every two seconds.

### Rename detection

A Studio rename appears as one path vanishing and another appearing. If exactly
one vanished path and exactly one new path share a content hash, the pair is
turned into a single `rename` decision and the local file is *moved* — with the
previous copy preserved under `backups/renames/<timestamp>/`. Ambiguous matches
are deliberately left as delete-plus-create: guessing could move the wrong file.

---

## 5. Durability

* **Atomic writes** — write to `<name>.rss-tmp`, then `os.replace`. A reader
  never sees a half-written file, and the temp name cannot be mistaken for a
  script because it does not end in `.luau`.
* **Atomic state** — `sync-state.json` is written to a temp file and replaced.
* **Settle window** — a local file is pushed only once its mtime is at least
  `settleSeconds` old, measured from the filesystem rather than an in-process
  counter so a one-shot `rss sync` behaves the same as the daemon.
* **Read-back verification** — pushes re-read from Studio, pulls re-read from
  disk. A write is recorded as synced only if the read-back matches.
* **Audit trail** — `logs/audit.jsonl` is append-only, one JSON object per
  event, flushed and fsynced. It records both sides' hashes for every change.

### Cloud-synced folders

Development happened in a OneDrive-redirected folder, so `src/` was synced to the
cloud the whole time. That is handled rather than ignored, and the handling is
worth keeping if your project folder is synced by anything: writes are atomic so
the sync client never uploads a torn file, hashing is content-based so a delayed
or repeated write is a no-op, and the sync client's own conflict copies
(`*conflict*`) are excluded by the ignore list. A CRLF difference introduced by
any tool is absorbed by the canonical form.

---

## 6. Defects found by independent verification, and their fixes

Two verifier agents audited this project adversarially, using their own tooling
rather than the project's. They confirmed the core claims and found eight real
defects. All are fixed, each with a regression test where one is possible.

| # | Defect | Fix |
| --- | --- | --- |
| 1 | **Unbounded conflict copies.** `_do_conflict` minted a fresh timestamped file every poll, forever — measured 12 copies in 20 s, ~43 000/day, in a cloud-synced folder. | `conflictSignature` makes repeats a no-op; a changed signature supersedes the previous copy. `tests/test_conflict_bounded.py` |
| 2 | **`resolve` deleted other scripts' copies.** A recursive glob filtered by `name.startswith(stem)` meant resolving `Foo` also deleted `FooBar.conflict-*.luau`. | Glob scoped to the exact stem in the same directory. |
| 3 | **The "32-bit FNV-1a" was not FNV-1a.** `h * 16777619` reaches ~2^56; Roblox's `bit32` returns doubles, so low bits were rounded away (~28 effective bits; disagreed with real FNV-1a on 66/66 scripts). | Multiply split into 16-bit halves keeps every intermediate under 2^41. `tests/test_codegen_edges.py` asserts equality against an independent Python FNV-1a. |
| 4 | **`_render` rejected legitimate scripts.** Its completeness check scanned the substituted *output*, so any script containing `@@UPPERCASE@@` could never sync. | Check the template only; `_TOKEN_RE.sub` already raises on unbound markers. |
| 5 | **Fetch retry missed two failure modes.** Split-and-retry keyed on `"truncat"`, so `[Complex Table]` and `StudioOpsError` aborted the whole cycle and were reported as "studio unreachable". | Catch both types; split on any payload-shaped failure. |
| 6 | **`lua_quote` escapes could merge with a digit.** `\9` followed by `5` reads as `\95` = `_`. | Zero-pad to three digits; Lua stops after three. |
| 7 | **A class change orphaned a script.** Retyping `ModuleScript`→`Script` in Studio gives a new filename suffix, producing `local-missing` *and* `studio-missing` for the same path — no write kind, so it silently stopped syncing. | New `retype` decision moves the file and clears the detector hash so content is revalidated. |
| 8 | **`lua_long_string` closed early when text ended in `]`.** `text + "]]"` ends in `]]]`, so the lexer closed one character early — reachable for any script whose last line ends in `]` with no trailing newline. | Choose the level by locating the closer's *first* occurrence in the finished literal; it must sit at the final position. `tests/test_codegen_edges.py` |

Defect 8 was found while fixing the others and had been missed by both
verifiers — the escape layer deserves its own adversarial tests rather than
reasoning.

Two further hardening changes came from the same review: `write_sources` now
refuses an **unguarded** write when no expected hash is supplied (previously it
silently degraded to an unconditional overwrite for callers other than the
daemon), and `_do_rename` refuses to overwrite an existing destination. `rss
sync` and `rss watch` now take a shared exclusive lock, because two processes
mutating one tree produces exactly the interleaving that looks like churn.

A note on concurrency: one verifier's first loop-prevention run was contaminated
because this project's own test suites were running against the same Studio
place at the same time. That is not a defect in the deliverable, but it is why
the lock exists and why the live suites must be run sequentially.

## 7. Module map

| Module | Responsibility |
| --- | --- |
| `tools/init.py` | first-run setup: settings, Rojo file, folders, interpreter record |
| `tools/_pycheck.py` | interpreter gate the `.cmd` launchers probe with |
| `tools/mcp_client.py` | stdio JSON-RPC client for `StudioMCP.exe`, reconnect, strict decode |
| `tools/studio_ops.py` | Luau codegen, snapshot, sliced reads, compare-and-swap writes, create, verified delete |
| `tools/sync_engine.py` | path mapping, hashing, `SyncState`, the decision table (pure) |
| `tools/daemon.py` | one sync cycle, apply decisions, verification, reconnect loop |
| `tools/project.py` | project discovery and settings |
| `tools/rslog.py` | dated log + append-only audit trail |
| `tools/backup.py` | timestamped snapshots of both sides |
| `tools/rss.py` | CLI |

**Structural safety:** the daemon has no code path that deletes anything, in
either direction. `studio_ops.delete_script` requires the literal token
`I UNDERSTAND THIS DELETES FROM STUDIO` and is reachable only from an explicit
CLI/test invocation.
