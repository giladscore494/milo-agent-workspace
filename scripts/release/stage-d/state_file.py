"""Machine-readable Stage D run state.

The single handoff between 05-execute-run.sh, 06-collect-evidence.sh and
the cleanup trap in 02-guarded-run.md. Nothing downstream may depend on an
operator copying a run id out of a terminal: a mistyped id would point the
evidence gate at the wrong run, and a lost id would leave a cleanup unable
to name the run it must account for.

Writes are atomic (temp file + os.replace) so a crash mid-write can never
leave a half-written state that a later reader would silently misparse.
Reads NEVER fail: a missing, unreadable or malformed file yields an empty
value, and the caller fails closed on emptiness. That asymmetry is
deliberate — a reader that raised would take down a cleanup trap.

Usage:
  python3 state_file.py <path> write <key> <value>
  python3 state_file.py <path> read  <key>     # prints the value, or nothing
"""

from __future__ import annotations

import json
import os
import sys


def load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: state_file.py <path> write <key> <value> | <path> read <key>", file=sys.stderr)
        return 2
    path, mode, key = sys.argv[1], sys.argv[2], sys.argv[3]
    if mode == "read":
        value = load(path).get(key)
        if value:
            print(value)
        return 0
    if mode == "write":
        if len(sys.argv) < 5:
            print("usage: state_file.py <path> write <key> <value>", file=sys.stderr)
            return 2
        state = load(path)
        state[key] = sys.argv[4]
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
        return 0
    print(f"unknown mode {mode!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
