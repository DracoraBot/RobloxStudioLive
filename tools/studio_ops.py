"""
RobloxStudioLive - Studio object/script operations over MCP.

All Studio mutation and inspection funnels through here.  Nothing else in this
project writes to Studio.

Safety properties enforced structurally
---------------------------------------
1.  There is no "delete" in the sync path.  `delete_script` exists only for an
    explicit human-invoked command and demands an exact confirmation token.
2.  Every write is *compare-and-swap*: the caller supplies the Studio-side hash
    it last observed.  If Studio changed underneath us the write is refused and
    reported as a conflict rather than clobbering someone else's edit.

Codegen rules (learned the hard way -- see docs/ARCHITECTURE.md)
---------------------------------------------------------------
* Python values are embedded into Luau with `lua_value()`, which emits **Luau
  literals**.  JSON is NOT valid Lua: a JSON array `["a"]` is an index
  expression in Lua, and JSON object keys use `:` not `=`.
* Luau is assembled by single-pass `@@TOKEN@@` substitution, never `%`
  formatting, because script sources contain `%`, `{}` and backslashes.
* Lua's lexer normalises CR/CRLF to LF inside long strings, so text containing
  a carriage return is emitted as split long-string segments joined by an
  explicit `"\\r"` escape.
* Luau tables returned across the bridge are re-encoded as JSON objects with
  stringified integer keys, and a table above roughly 90 KB comes back as the
  literal placeholder `"[Complex Table]"`.  Every read therefore returns
  `HttpService:JSONEncode(payload)` -- a plain string, which the bridge passes
  through intact to at least 800 KB and which preserves JSON array semantics.
* Every read returns a **path-keyed mapping**, never a positional array.

Public API (frozen -- see docs/ARCHITECTURE.md):
    SCRIPT_CLASSES, DEFAULT_ROOTS, DELETE_CONFIRM_TOKEN
    lua_value(v) -> str
    lua_quote(s) -> str
    lua_string_literal(s) -> str
    lua_long_string(s) -> str

    snapshot_scripts(client, roots) -> dict[path, meta]
    fetch_sources(client, paths, sizes=None) -> dict[path, source]
    write_sources(client, items) -> dict[path, result]
    verify_sources(client, items) -> dict[path, result]
    create_script(client, parent_path, name, class_name, source) -> dict
    script_exists(client, path) -> bool
    instance_tree(client, root, max_depth) -> dict[path, node]
    delete_script(client, path, confirm_token) -> dict
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from mcp_client import StudioMCPError

__all__ = [
    "StudioOpsError",
    "SCRIPT_CLASSES",
    "DEFAULT_ROOTS",
    "DELETE_CONFIRM_TOKEN",
    "lua_value",
    "lua_quote",
    "lua_string_literal",
    "lua_long_string",
    "snapshot_scripts",
    "fetch_sources",
    "write_sources",
    "verify_sources",
    "create_script",
    "script_exists",
    "instance_tree",
    "delete_script",
]


class StudioOpsError(RuntimeError):
    """The bridge returned something we cannot interpret as a result payload."""

SCRIPT_CLASSES = ("Script", "LocalScript", "ModuleScript")
DEFAULT_ROOTS = [
    "ServerScriptService",
    "ReplicatedStorage",
    "StarterGui",
    "StarterPlayer",
    "ReplicatedFirst",
    "ServerStorage",
    "Workspace",
]
DELETE_CONFIRM_TOKEN = "I UNDERSTAND THIS DELETES FROM STUDIO"

# Max payload pushed into one execute_luau call before splitting into batches.
# Input is not hard-capped (400 000 chars was observed working), but keeping
# calls modest keeps latency predictable.
MAX_CALL_BYTES = 300_000

# The Studio bridge truncates a tool RESPONSE at ~100 000 characters and
# replaces a returned Luau table above ~90 KB with the literal
# "[Complex Table]".  Reads therefore come back as JSON strings, sliced to fit
# comfortably inside that ceiling.
RESPONSE_CHAR_LIMIT = 100_000
SAFE_RESPONSE_BUDGET = 76_000
SLICE_BYTES = 45_000
# JSON escaping inflates source text a little (newlines, quotes, backslashes).
INFLATION = 1.5
DEFAULT_SIZE_HINT = 20_000

_TOKEN_RE = re.compile(r"@@([A-Z][A-Z0-9_]*)@@")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LUA_KEYWORDS = frozenset(
    """and break do else elseif end false for function if in local nil not or
    repeat return then true until while continue export type self""".split()
)


def _render(template: str, **values: str) -> str:
    """Single-pass substitution of @@TOKEN@@ markers.

    Single pass matters, and the completeness check must be applied to the
    *template*, never to the result.  Substituted values are script bodies,
    which legitimately contain things that look like tokens: a script holding
    `@@HELLO@@` must not be mistaken for an unbound template marker.
    `_TOKEN_RE.sub` visits every marker in the template exactly once and
    `_sub` raises on any that has no binding, so no second pass is needed.
    """
    def _sub(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            raise ValueError("unbound template token @@%s@@" % key)
        return values[key]

    return _TOKEN_RE.sub(_sub, template)


# --------------------------------------------------------------------------
# String / value literals
# --------------------------------------------------------------------------
def lua_quote(text: str) -> str:
    """Arbitrary text as a Luau double-quoted literal (exact for all bytes)."""
    out = ['"']
    for ch in text:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif code < 0x20 or code == 0x7F:
            # Zero-padded to three digits on purpose: Lua reads at most three
            # digits after a backslash, so a 3-digit escape can never merge
            # with a following digit.  "\\%d" % 9 followed by the character
            # "5" would emit \95 and decode as "_" instead of tab.
            out.append("\\%03d" % code)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def lua_long_string(text: str) -> str:
    """Text as a Luau long string.  Assumes `text` contains no CR (see below).

    The level is chosen so the closing bracket cannot be matched early.  Note
    that checking `closer not in text` is NOT sufficient: if the text ends with
    `]` and the closer is `]]`, the emitted `text + "]]"` ends in `]]]` and the
    lexer closes the string one character early, leaving a stray `]` that is a
    parse error.

    The test is therefore "where does the closer FIRST occur in the finished
    literal?".  It must occur exactly at the final position; anything earlier
    (including a match straddling the text/closer boundary) would close the
    string prematurely.  Using `str.find` rather than `str.count` matters
    because `count` is non-overlapping and would under-report `]]` in `]]]`.
    """
    level = 0
    while level <= 24:
        opener = "[" + "=" * level + "["
        closer = "]" + "=" * level + "]"
        body = opener + "\n" + text
        if (body + closer).find(closer) == len(body):
            # Lua skips exactly one newline after the opener, so we always emit
            # one ourselves; the decoded value is then byte-exact.
            return body + closer
        level += 1
    raise ValueError("no safe long-bracket level available for this text")


def lua_string_literal(text: str) -> str:
    """Byte-exact Luau literal for arbitrary text, of any length.

    Lua's lexer folds CR, LF and CRLF to a single LF inside long strings, so
    text containing a carriage return is emitted as long-string segments joined
    with an explicit "\\r" escape.  Everything else uses a long string, which
    avoids escaping large script bodies.
    """
    if "\r" not in text:
        return lua_long_string(text)
    segments = text.split("\r")
    parts = [lua_long_string(seg) for seg in segments]
    return ' .. "\\r" .. '.join(parts)


def lua_value(obj: Any) -> str:
    """Serialise a Python value as a **Luau literal** (not JSON)."""
    if obj is None:
        return "nil"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if isinstance(obj, str):
        return lua_string_literal(obj)
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, float):
        return repr(obj)
    if isinstance(obj, dict):
        parts = []
        for key, val in obj.items():
            name = key if isinstance(key, str) else str(key)
            if _IDENT_RE.match(name) and name not in _LUA_KEYWORDS:
                parts.append("%s = %s" % (name, lua_value(val)))
            else:
                # Always index with a quoted literal, never a long string:
                # "[ " + "[[...]]" would lex as the ambiguous token "[[[".
                parts.append("[%s] = %s" % (lua_quote(name), lua_value(val)))
        return "{" + ", ".join(parts) + "}"
    if isinstance(obj, (list, tuple)):
        return "{" + ", ".join(lua_value(v) for v in obj) + "}"
    raise TypeError("cannot serialise %r to Luau" % type(obj).__name__)


def _as_mapping(result: Any, key_field: str = "path") -> dict:
    """Normalise a bridge result into a mapping.

    Reads are returned as `HttpService:JSONEncode(...)` strings, so `result` is
    normally already a decoded dict.  Anything else is an error we refuse to
    paper over -- silently returning {} here would look like "Studio has no
    scripts" and could cascade into destructive behaviour downstream.
    """
    if result is None:
        raise StudioOpsError("bridge returned no result payload")
    if isinstance(result, str):
        if result.strip() == "[Complex Table]":
            raise StudioOpsError(
                "response was replaced by the '[Complex Table]' placeholder; "
                "the read must be chunked smaller"
            )
        raise StudioOpsError("expected a mapping from the bridge, got text: %r" % result[:200])
    if isinstance(result, dict):
        # Defensive: a positional array would arrive as {"1": item, ...}
        if result and all(str(k).isdigit() for k in result):
            listed = [result[k] for k in sorted(result, key=lambda x: int(x))]
            return {
                item[key_field]: item
                for item in listed
                if isinstance(item, dict) and key_field in item
            }
        return dict(result)
    if isinstance(result, list):
        return {
            item[key_field]: item
            for item in result
            if isinstance(item, dict) and key_field in item
        }
    raise StudioOpsError("unexpected bridge result type %s" % type(result).__name__)


# --------------------------------------------------------------------------
# Shared Luau prelude
# --------------------------------------------------------------------------
# 32-bit FNV-1a, used only as a *cheap change detector* so a poll can learn
# "something changed" without shipping ~400 KB of source over the wire.
# Python re-hashes every fetched source with SHA-256 -- the authoritative
# digest -- so a collision here is harmless.
_LUA_PRELUDE = r"""
-- Exact 32-bit FNV-1a.
--
-- The multiply is split into 16-bit halves on purpose.  Roblox's bit32
-- returns ordinary numbers (IEEE doubles), so `h * 16777619` with h < 2^32
-- reaches ~2^56 -- past the 2^53 exact-integer range -- and the low bits get
-- rounded away.  Measured: that naive form disagreed with real FNV-1a on
-- 66/66 scripts and produced a hash ending in a zero nibble, i.e. ~28
-- effective bits instead of 32.  Splitting keeps every intermediate under
-- 2^41 and therefore exact.
local function rssMul32(a)
	local lo = a % 65536
	local hi = (a - lo) / 65536
	return (lo * 16777619 + ((hi * 16777619) % 65536) * 65536) % 4294967296
end

local function rssHash(s)
	local h = 2166136261
	local n = #s
	local i = 1
	local bx = bit32.bxor
	while i <= n do
		local c1, c2, c3, c4 = string.byte(s, i, i + 3)
		if c1 then h = rssMul32(bx(h, c1)) end
		if c2 then h = rssMul32(bx(h, c2)) end
		if c3 then h = rssMul32(bx(h, c3)) end
		if c4 then h = rssMul32(bx(h, c4)) end
		i = i + 4
	end
	return string.format("%08x:%d", h, n)
end

local function rssSourceOf(inst)
	local ok, src = pcall(function() return inst.Source end)
	if ok and type(src) == "string" then return src end
	return nil
end

local function rssResolve(pathStr)
	local cur = game
	for _, part in ipairs(string.split(pathStr, ".")) do
		local nxt = cur:FindFirstChild(part)
		if not nxt then return nil end
		cur = nxt
	end
	return cur
end

local function rssFullPath(inst)
	local parts = {}
	local node = inst
	while node and node ~= game do
		table.insert(parts, 1, node.Name)
		node = node.Parent
	end
	return table.concat(parts, ".")
end

local function rssRecord(inst)
	local src = rssSourceOf(inst)
	if src == nil then return nil end
	local parent = inst.Parent
	return {
		path = rssFullPath(inst),
		name = inst.Name,
		className = inst.ClassName,
		parentPath = parent and rssFullPath(parent) or "",
		fnv = rssHash(src),
		bytes = #src,
	}
end

-- Results are returned as a JSON *string*, not as a Luau table.  The bridge
-- replaces a table above ~90 KB with the literal "[Complex Table]" and
-- re-encodes Lua arrays as objects keyed "1","2",...; a JSON string sidesteps
-- both and survives to at least 800 KB.
local function rssJSON(t)
	local ok, s = pcall(function()
		return game:GetService("HttpService"):JSONEncode(t)
	end)
	if ok and type(s) == "string" then return s end
	return '{"__rssError":"json-encode-failed"}'
end
"""


def _wrap(body: str) -> str:
    return _LUA_PRELUDE + "\n" + body


# --------------------------------------------------------------------------
# read: cheap metadata poll
# --------------------------------------------------------------------------
_SNAPSHOT_TEMPLATE = r"""
local ROOTS = @@ROOTS@@
local out = {}
for _, rootName in ipairs(ROOTS) do
	local root = game:FindFirstChild(rootName)
	if root then
		local ok, descendants = pcall(function() return root:GetDescendants() end)
		if ok then
			for _, inst in ipairs(descendants) do
				if inst:IsA("LuaSourceContainer") then
					local rec = rssRecord(inst)
					if rec ~= nil then
						out[rec.path] = rec
					end
				end
			end
		end
	end
end
return rssJSON(out)
"""


def snapshot_scripts(
    client: Any,
    roots: Iterable[str] | None = None,
    include_workspace: bool = True,
) -> dict[str, dict]:
    """Metadata + change-detector hash for every script under `roots`.

    Deliberately returns no source text: a 2-second poll must stay cheap.
    Fetch full text only for entries whose `fnv` differs from the last sync.
    Returns {studio_path: {path, name, className, parentPath, fnv, bytes}}.
    """
    roots = list(roots or DEFAULT_ROOTS)
    if not include_workspace:
        roots = [r for r in roots if r != "Workspace"]
    code = _render(_SNAPSHOT_TEMPLATE, ROOTS=lua_value(roots))
    return _as_mapping(client.execute_luau(_wrap(code)))


# --------------------------------------------------------------------------
# read: full source
# --------------------------------------------------------------------------
_FETCH_TEMPLATE = r"""
local SPECS = @@SPECS@@
local out = {}
for _, spec in ipairs(SPECS) do
	local inst = rssResolve(spec.path)
	if inst and inst:IsA("LuaSourceContainer") then
		local s = rssSourceOf(inst) or ""
		local len = #s
		local from = spec.from
		local to = spec.to
		if to < 0 or to > len then to = len end
		-- Keep every slice on a UTF-8 character boundary: the JSON round trip
		-- would otherwise see a half-formed character, replace it, and corrupt
		-- the script text.
		if to < len then
			local b = string.byte(s, to + 1)
			while to > from and b and b >= 128 and b <= 191 do
				to = to - 1
				b = string.byte(s, to + 1)
			end
		end
		out[spec.key] = {
			key = spec.key, path = spec.path, ok = true,
			from = from, part = string.sub(s, from, to), len = len,
			-- Hash of the WHOLE source, not just this slice.  Reassembling
			-- slices from two different generations of a script would still
			-- add up to the right byte count if an edit preserved its length,
			-- so all slices of one path must agree on this.
			srcFnv = rssHash(s),
		}
	else
		out[spec.key] = { key = spec.key, path = spec.path, ok = false }
	end
end
return rssJSON(out)
"""


_SIZE_TEMPLATE = r"""
local PATHS = @@PATHS@@
local out = {}
for _, p in ipairs(PATHS) do
	local inst = rssResolve(p)
	if inst and inst:IsA("LuaSourceContainer") then
		out[p] = { path = p, len = #(rssSourceOf(inst) or "") }
	end
end
return rssJSON(out)
"""


def probe_sizes(client: Any, paths: list[str]) -> dict[str, int]:
    """Cheap `{path: byte length}` probe so reads can be sliced exactly.

    Costs ~100 bytes per script, which beats guessing: a wrong guess either
    truncates a large script or wastes round trips on small ones.
    """
    if not paths:
        return {}
    sizes: dict[str, int] = {}
    for chunk in _chunk(paths, 120):
        code = _render(_SIZE_TEMPLATE, PATHS=lua_value(chunk))
        result = _as_mapping(client.execute_luau(_wrap(code)))
        for path, entry in result.items():
            if isinstance(entry, dict):
                sizes[path] = int(entry.get("len") or 0)
    return sizes


def _chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _run_fetch_call(client: Any, specs: list[dict], depth: int = 0) -> dict:
    """Run one fetch call, halving the request if the bridge truncates it.

    The bridge truncates a response at ~100 000 characters.  Our planner keeps
    well clear of that, but a single unusually escape-heavy script could still
    trip it, so a truncated call is retried in halves rather than failing the
    whole sync cycle.
    """
    code = _render(_FETCH_TEMPLATE, SPECS=lua_value(specs))
    try:
        return _as_mapping(client.execute_luau(_wrap(code)), key_field="key")
    except (StudioMCPError, StudioOpsError) as exc:
        # An oversized payload can surface three different ways: a truncated
        # JSON string, the bridge's "[Complex Table]" placeholder, or
        # StudioOpsError from _as_mapping.  All three mean "this call carried
        # too much", so retry the half rather than aborting the whole cycle --
        # an abort here would be reported as "studio unreachable", which is the
        # wrong cause and would hide a perfectly healthy connection.
        message = str(exc).lower()
        oversized = ("truncat" in message or "complex table" in message
                     or "invalid json" in message)
        if depth < 4 and len(specs) > 1 and oversized:
            mid = len(specs) // 2
            merged = _run_fetch_call(client, specs[:mid], depth + 1)
            merged.update(_run_fetch_call(client, specs[mid:], depth + 1))
            return merged
        raise


def fetch_sources(
    client: Any,
    paths: list[str],
    sizes: dict[str, int] | None = None,
    budget: int = SAFE_RESPONSE_BUDGET,
) -> dict[str, str]:
    """Exact source text for the given Studio script paths (missing -> absent).

    Reads are planned against the bridge's ~100 000-character response cap:
    small scripts are packed together, anything larger is transferred as
    UTF-8-safe byte slices and reassembled here.  A read that does not add up
    raises instead of returning partial text -- partial text would otherwise be
    written back over good code.
    """
    if not paths:
        return {}

    # Fill in any sizes the caller did not already know, so every slice boundary
    # is chosen from real data rather than a guess.
    known = dict(sizes or {})
    unknown = [p for p in paths if p not in known]
    if unknown:
        known.update(probe_sizes(client, unknown))

    out: dict[str, str] = {}
    collected: dict[str, dict] = {}

    for specs in _plan_fetch(paths, known, budget):
        result = _run_fetch_call(client, specs)
        for spec in specs:
            entry = result.get(spec["key"])
            if not isinstance(entry, dict) or not entry.get("ok"):
                continue
            path = entry.get("path") or spec["path"]
            bucket = collected.setdefault(
                path, {"len": entry.get("len"), "parts": [], "srcFnv": entry.get("srcFnv")})
            if entry.get("srcFnv") and bucket["srcFnv"] and entry["srcFnv"] != bucket["srcFnv"]:
                raise StudioOpsError(
                    "read of %s spans two different versions of the script "
                    "(it changed while being read); retry next cycle" % path)
            bucket["parts"].append((int(entry.get("from", 0)), entry.get("part") or ""))

    for path in paths:
        bucket = collected.get(path)
        if bucket is None:
            continue
        parts = sorted(bucket["parts"], key=lambda t: t[0])
        text = "".join(p[1] for p in parts)
        declared = bucket["len"]
        if declared is not None and len(text.encode("utf-8")) != declared:
            raise StudioOpsError(
                "read of %s is inconsistent: reassembled %d bytes, Studio reported %d "
                "(the script most likely changed mid-read; retry next cycle)"
                % (path, len(text.encode("utf-8")), declared)
            )
        out[path] = text

    missing = [p for p in paths if p not in out]
    if missing:
        raise StudioOpsError(
            "fetch returned nothing for %d of %d requested scripts, e.g. %s"
            % (len(missing), len(paths), missing[:3])
        )
    return out


def _plan_fetch(
    paths: list[str],
    sizes: dict[str, int],
    budget: int,
) -> list[list[dict]]:
    """Group slice specs so each inner list fits in one MCP call.

    Slices of one large script are spread across *separate* calls -- packing a
    140 KB script's three slices together would blow the response cap even
    though each slice is individually small.
    """
    calls: list[list[dict]] = []
    packed: list[dict] = []
    packed_cost = 0.0

    def add(spec: dict, cost: float) -> None:
        nonlocal packed, packed_cost
        if packed and packed_cost + cost > budget:
            calls.append(packed)
            packed, packed_cost = [], 0.0
        packed.append(spec)
        packed_cost += cost

    for path in paths:
        # A path absent from the probe does not exist (or appeared after it).
        # Bound the request instead of asking for "everything": if it turns out
        # to be huge, the length check below catches the short read loudly
        # rather than silently importing a truncated script.
        size = int(sizes[path]) if path in sizes else DEFAULT_SIZE_HINT
        size = max(1, size)
        if size <= SLICE_BYTES:
            add({"key": path, "path": path, "from": 1,
                 "to": -1 if path in sizes else DEFAULT_SIZE_HINT},
                size * INFLATION + 400)
            continue

        start = 1
        while start <= size:
            end = start + SLICE_BYTES - 1
            last = end >= size
            raw = (size - start + 1) if last else SLICE_BYTES
            add(
                {
                    "key": "%s@%d" % (path, start),
                    "path": path,
                    "from": start,
                    "to": -1 if last else end,
                },
                raw * INFLATION + 400,
            )
            start = end + 1

    if packed:
        calls.append(packed)
    return calls


# --------------------------------------------------------------------------
# write: compare-and-swap
# --------------------------------------------------------------------------
_WRITE_TEMPLATE = r"""
local META = @@META@@
local SRC = {}
local it = {}
@@ASSIGNS@@
local out = {}

for i, m in ipairs(META) do
	local src = it[i]
	local entry = { path = m.path, ok = false, status = "error" }
	local inst = rssResolve(m.path)

	if not inst and m.create and m.parentPath then
		local parent = rssResolve(m.parentPath)
		if parent then
			local okNew, created = pcall(function()
				local n = Instance.new(m.className)
				n.Name = m.path:match("[^.]*$")
				n.Source = src
				n.Parent = parent
				return n
			end)
			if okNew and created then
				inst = created
				entry.status = "created"
				entry.ok = true
			else
				entry.detail = "create failed: " .. tostring(created)
			end
		else
			entry.status = "missing-parent"
			entry.detail = "parent not found: " .. tostring(m.parentPath)
		end
	end

	if inst and inst:IsA("LuaSourceContainer") then
		local currentFnv = rssHash(rssSourceOf(inst) or "")
		entry.previousFnv = currentFnv
		if entry.status == "created" then
			-- content already applied during creation
		elseif m.expectedFnv == nil and not m.allowUnguarded then
			-- No hash to compare against: refuse rather than silently
			-- degrading into an unconditional overwrite.  Callers that
			-- genuinely have nothing to compare (none in the sync path)
			-- must opt in explicitly.
			entry.status = "refused"
			entry.detail = "no expected hash supplied; refusing an unguarded write"
		elseif m.expectedFnv ~= nil and m.expectedFnv ~= currentFnv then
			entry.status = "conflict"
			entry.detail = "studio content changed since it was last read"
		else
			local chs = game:GetService("ChangeHistoryService")
			local recording = false
			pcall(function() chs:TryBeginRecording("RobloxStudioLive sync"); recording = true end)
			local okSet, err = pcall(function() inst.Source = src end)
			if recording then pcall(function() chs:FinishRecording("RobloxStudioLive sync") end) end
			if okSet then
				entry.ok = true
				entry.status = "written"
			else
				entry.status = "error"
				entry.detail = "write failed: " .. tostring(err)
			end
		end
		local after = rssSourceOf(inst)
		entry.newFnv = rssHash(after or "")
		entry.bytes = #(after or "")
	elseif entry.status ~= "missing-parent" then
		entry.status = "missing"
		entry.detail = entry.detail or "script not found in studio"
	end

	out[m.path] = entry
end
return rssJSON(out)
"""


def write_sources(client: Any, items: list[dict]) -> dict[str, dict]:
    """Apply sources to Studio with compare-and-swap protection.

    Each item: {"path", "source", "expected_fnv"|None, "create": bool,
                "parent_path", "class_name"}

    Returns {path: {"ok","status","detail","previousFnv","newFnv","bytes"}}.
    status in: written | created | conflict | missing | missing-parent | error
    """
    results: dict[str, dict] = {}
    for batch in _batch_writes(items):
        results.update(_write_batch(client, batch))
    return results


def _batch_writes(items: list[dict], limit: int = MAX_CALL_BYTES) -> list[list[dict]]:
    """Group writes so one MCP call never carries an unreasonable payload."""
    batches: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for item in items:
        cost = len(item.get("source") or "") + 500
        if current and size + cost > limit:
            batches.append(current)
            current, size = [], 0
        current.append(item)
        size += cost
    if current:
        batches.append(current)
    return batches


def _write_batch(client: Any, items: list[dict]) -> dict[str, dict]:
    meta = []
    assigns = []
    for idx, item in enumerate(items, start=1):
        source = item.get("source") or ""
        meta.append(
            {
                "path": item["path"],
                "expectedFnv": item.get("expected_fnv"),
                "create": bool(item.get("create")),
                "parentPath": item.get("parent_path"),
                "className": item.get("class_name") or "Script",
                "allowUnguarded": bool(item.get("allow_unguarded")),
            }
        )
        assigns.append("it[%d] = %s" % (idx, lua_string_literal(source)))

    code = _render(_WRITE_TEMPLATE, META=lua_value(meta), ASSIGNS="\n".join(assigns))
    return _as_mapping(client.execute_luau(_wrap(code)))


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------
_VERIFY_TEMPLATE = r"""
local META = @@META@@
local out = {}
for _, m in ipairs(META) do
	local inst = rssResolve(m.path)
	if inst and inst:IsA("LuaSourceContainer") then
		local src = rssSourceOf(inst) or ""
		local f = rssHash(src)
		out[m.path] = {
			path = m.path,
			fnv = f,
			match = (m.expectedFnv == nil) or (m.expectedFnv == f),
			bytes = #src,
		}
	else
		out[m.path] = { path = m.path, match = false, bytes = 0, missing = true }
	end
end
return rssJSON(out)
"""


def verify_sources(client: Any, items: list[dict]) -> dict[str, dict]:
    """Independently re-read Studio and report the hash actually stored there."""
    if not items:
        return {}
    payload = [{"path": i["path"], "expectedFnv": i.get("expected_fnv")} for i in items]
    code = _render(_VERIFY_TEMPLATE, META=lua_value(payload))
    return _as_mapping(client.execute_luau(_wrap(code)))


# --------------------------------------------------------------------------
# create / inspect
# --------------------------------------------------------------------------
_CREATE_TEMPLATE = r"""
local PARENT_PATH = @@PARENT@@
local NAME = @@NAME@@
local CLASS = @@CLASS@@
local SRC = @@SRC@@

local cur = game
for _, part in ipairs(string.split(PARENT_PATH, ".")) do
	local nxt = cur:FindFirstChild(part)
	if not nxt then
		return rssJSON({ ok = false, status = "missing-parent", detail = "no parent " .. PARENT_PATH })
	end
	cur = nxt
end

if cur:FindFirstChild(NAME) then
	return rssJSON({ ok = false, status = "exists", detail = "an instance named " .. NAME .. " already exists under " .. PARENT_PATH })
end

local ok, res = pcall(function()
	local inst = Instance.new(CLASS)
	inst.Name = NAME
	inst.Source = SRC
	inst.Parent = cur
	return inst
end)

if not ok then
	return rssJSON({ ok = false, status = "error", detail = tostring(res) })
end

local inst = res
local src = rssSourceOf(inst) or ""
return rssJSON({
	ok = true,
	status = "created",
	path = PARENT_PATH .. "." .. NAME,
	className = inst.ClassName,
	fnv = rssHash(src),
	bytes = #src,
})
"""


def create_script(
    client: Any,
    parent_path: str,
    name: str,
    class_name: str,
    source: str = "",
) -> dict:
    """Create a script.  Refuses if the target already exists (no duplicates)."""
    if class_name not in SCRIPT_CLASSES:
        raise ValueError("class_name must be one of %s" % (SCRIPT_CLASSES,))
    code = _render(
        _CREATE_TEMPLATE,
        PARENT=lua_string_literal(parent_path),
        NAME=lua_string_literal(name),
        CLASS=lua_string_literal(class_name),
        SRC=lua_string_literal(source),
    )
    result = client.execute_luau(_wrap(code))
    return dict(result) if isinstance(result, dict) else {}


_EXISTS_TEMPLATE = r"""
local inst = rssResolve(@@PATH@@)
return rssJSON({
	exists = (inst ~= nil and inst:IsA("LuaSourceContainer")),
	className = inst and inst.ClassName or nil,
})
"""


def script_exists(client: Any, path: str) -> bool:
    code = _render(_EXISTS_TEMPLATE, PATH=lua_string_literal(path))
    result = client.execute_luau(_wrap(code))
    return bool(result.get("exists")) if isinstance(result, dict) else False


_TREE_TEMPLATE = r"""
local root = @@ROOT@@
if not root then return rssJSON({}) end
local maxDepth = @@DEPTH@@
local out = {}
local function walk(inst, depth, path)
	if depth > maxDepth then return end
	for _, ch in ipairs(inst:GetChildren()) do
		local p = path .. "." .. ch.Name
		out[p] = {
			path = p, name = ch.Name, className = ch.ClassName,
			parentPath = path, depth = depth,
			childCount = #ch:GetChildren(),
			isScript = ch:IsA("LuaSourceContainer"),
		}
		walk(ch, depth + 1, p)
	end
end
walk(root, 1, root.Name or "game")
return rssJSON(out)
"""


def instance_tree(client: Any, root: str = "game", max_depth: int = 3) -> dict[str, dict]:
    """Flat instance tree (names/classes) -- lets prompts 'see' the hierarchy."""
    root_expr = "game" if root in ("game", "", None) else "rssResolve(%s)" % lua_string_literal(root)
    code = _render(_TREE_TEMPLATE, ROOT=root_expr, DEPTH=str(int(max_depth)))
    return _as_mapping(client.execute_luau(_wrap(code)))


# --------------------------------------------------------------------------
# destructive op -- never reachable from the sync daemon
# --------------------------------------------------------------------------
_DELETE_TEMPLATE = r"""
local inst = rssResolve(@@PATH@@)
if not inst then return rssJSON({ ok = false, status = "missing" }) end
if not inst:IsA("LuaSourceContainer") then return rssJSON({ ok = false, status = "not-a-script" }) end
local full = rssFullPath(inst)
local parentName = inst.Parent and inst.Parent:GetFullName() or nil
inst.Parent = nil
return rssJSON({ ok = true, status = "deleted", path = full, parent = parentName })
"""


def delete_script(client: Any, path: str, confirm_token: str) -> dict:
    """Delete a script from Studio.

    The daemon never calls this and no automatic path reaches it.  It requires
    the literal token in DELETE_CONFIRM_TOKEN.
    """
    if confirm_token != DELETE_CONFIRM_TOKEN:
        return {
            "ok": False,
            "status": "refused",
            "detail": "confirmation token required; nothing was deleted",
        }
    code = _render(_DELETE_TEMPLATE, PATH=lua_string_literal(path))
    result = client.execute_luau(_wrap(code))
    return dict(result) if isinstance(result, dict) else {}
