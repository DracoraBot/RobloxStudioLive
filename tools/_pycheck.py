"""
RobloxStudioLive - interpreter gate.

    python tools/_pycheck.py

Exits 0 when the interpreter running it is new enough to run this project, and
1 otherwise, with a one-line reason on stderr.

Why this file exists
--------------------
The `.cmd` launchers have to *test* a candidate interpreter before committing
to it, because on Windows two very different things both look like Python:

  * the Microsoft Store stub at
    `%LOCALAPPDATA%\\Microsoft\\WindowsApps\\python.exe`, which starts, prints
    an advert and exits non-zero - so `where python` succeeding proves nothing;
  * a genuinely old interpreter, which will not fail until something deep in an
    import gives a message that says nothing about the real problem.

Probing with a real script collapses both cases into one clear failure here,
before any of the project is loaded.

It imports only `os` and `sys`, and writes nothing: it has to be able to run on
the interpreters it is rejecting, and on a read-only checkout.
"""

import os
import sys

# Floor chosen from what the code actually uses, not from what is fashionable:
# no `match` statement, no `except*`, no 3.11+ stdlib, and every module carries
# `from __future__ import annotations`, so PEP 604 annotations (`str | None`)
# cost nothing at runtime.  3.9 is the oldest interpreter this project claims.
MINIMUM = (3, 9)


def _fail(message):
    sys.stderr.write(message + "\n")
    raise SystemExit(1)


if sys.version_info < MINIMUM:
    _fail(
        "RobloxStudioLive needs Python %d.%d or newer; this interpreter is %s."
        % (MINIMUM[0], MINIMUM[1], sys.version.split()[0])
    )

# A version number is not proof that the sources in *this* checkout parse.  A
# partially applied update, or a hand-edited file with a stray character, would
# otherwise surface much later as a confusing ImportError.  Compiling every
# source file in memory takes a few milliseconds, leaves no .pyc behind, and
# turns that into one honest line.
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

for _folder in ("tools", "tests"):
    _path = os.path.join(_root, _folder)
    if not os.path.isdir(_path):
        continue
    for _name in sorted(os.listdir(_path)):
        if not _name.endswith(".py"):
            continue
        _file = os.path.join(_path, _name)
        try:
            with open(_file, "rb") as _handle:
                _source = _handle.read()
        except OSError:
            continue  # unreadable is not a reason to refuse to start
        try:
            compile(_source, _file, "exec")
        except SyntaxError as _exc:
            _fail("RobloxStudioLive: %s does not compile: %s" % (_file, _exc))

raise SystemExit(0)
