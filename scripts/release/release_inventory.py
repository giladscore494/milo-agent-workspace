#!/usr/bin/env python3
"""The Production preflight inventory, DERIVED from the current repository.

What this replaces
------------------

Stage D's preflight carried a hand-written list of the RPCs it considered
required (``REQUIRED_RPC_ARGS`` in ``scripts/release/stage-d/probe_db.py``),
and that list was written when the guarded worker writes landed. Everything
built since -- the durable execution-usage ledger, atomic guarded
finalization, the current-verdict authority, the evidence support functions,
the catalog writers, and the run-identity and fencing primitives added
alongside this file -- became a RUNTIME DEPENDENCY without becoming a
PREFLIGHT REQUIREMENT. A production database missing
``record_run_usage_guarded`` or ``finalize_run_guarded`` would have passed
every Stage D check and then failed on the first paid model call, after the
money was spent.

A hand-maintained list of runtime dependencies will always eventually fall
behind the runtime. So this module does not keep one. It DERIVES the inventory,
every time it is asked, from two facts about the repository as it is now:

1.  WHAT THE RUNTIME CALLS. A static (AST / text) scan for every PostgREST RPC
    name this repository invokes -- the repository layer's guarded and read
    RPCs, and the RPCs the release tooling itself calls over ``/rest/v1/rpc/``.
    Nothing is read from a list; a call site is the evidence.
2.  WHAT THE MIGRATIONS PROVIDE. Every ``create [or replace] function
    public.<name>(...)`` in ``supabase/migrations``, with its parameters and
    which of them are REQUIRED (no ``default``). Later definitions of the same
    name supersede earlier ones, because that is what applying the migrations
    in order does.

The inventory is the intersection, and the two ways it can fail are both
blocking:

*   a runtime RPC with NO defining migration is a dependency production can
    never satisfy -- the code will call a function that was never created;
*   a required argument that the deployed signature does not advertise means
    the deployed function is not the one the migrations define.

Read-only and offline. It reads files, prints, and exits. No network, no
database, no gcloud, no mutation, and it never reads a credential.

Why the probe still carries a literal
-------------------------------------

``probe_db.py`` is transported into a bare, pinned container image as a single
SHA-256-pinned file and runs with the standard library alone, so it cannot
import this module. It therefore keeps the inventory as a reviewed literal --
the same arrangement as ``policy_envelope.PINNED_POLICY_FINGERPRINT`` -- and
``tests/test_release_inventory.py`` fails if that literal is not exactly what
this module derives from the current repository. The list is still generated;
what is reviewed is the act of updating it.

Usage:
  release_inventory.py rpcs        # name -> required argument names
  release_inventory.py migrations  # the migrations the required RPCs come from
  release_inventory.py json        # the whole inventory
  release_inventory.py verify      # exit non-zero if the inventory is unsound
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Where the runtime's own RPC calls live. Every module here is scanned with
#: the AST, so a name only counts when it is a literal at a real call site.
RUNTIME_SOURCES: tuple[str, ...] = (
    "backend/repository/supabase.py",
)

#: The release tooling's own RPC calls, which reach PostgREST over HTTP rather
#: than through the repository. They are runtime-required in the same sense:
#: the Stage D cleanup path releases dangling budget reservations through one,
#: and a cleanup that cannot run leaves the daily budget held.
TOOLING_SOURCES: tuple[str, ...] = (
    "scripts/release/stage-d/probe_db.py",
    "scripts/release/stage-c/probe_db.py",
)

#: RPC call shapes inside the repository layer. `_guarded_rpc` and `_read_rpc`
#: are this repository's own wrappers; `.rpc(` is the raw client call.
_RPC_CALLEES = ("_guarded_rpc", "_read_rpc", "rpc")

#: `/rest/v1/rpc/<name>` as the release tooling spells it.
_TOOLING_RPC_RE = re.compile(r"/rest/v1/rpc/([a-z0-9_]+)")

#: A migration's function definition header, up to the closing parenthesis of
#: its parameter list. Non-greedy so nested parentheses in a default value
#: cannot swallow the rest of the file.
_FUNCTION_RE = re.compile(
    r"create\s+(?:or\s+replace\s+)?function\s+public\.([a-z0-9_]+)\s*\((.*?)\)\s*returns",
    re.IGNORECASE | re.DOTALL)

#: Migration filenames, matching `scripts/release/migration_state.py` exactly.
_FILENAME_RE = re.compile(r"^(?P<version>[0-9]{3}|[0-9]{14})_[A-Za-z0-9][A-Za-z0-9_.-]*\.sql$")


class InventoryError(ValueError):
    """The inventory cannot be derived, which is never a pass."""


# ---------------------------------------------------------------------------
# what the migrations provide
# ---------------------------------------------------------------------------
def _split_params(raw: str) -> list[str]:
    """Split a parameter list on top-level commas only.

    A default like ``'{}'::jsonb`` is harmless, but ``coalesce(a, b)`` in one
    would split a single parameter into two and invent a required argument.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    in_string = False
    for char in raw:
        if char == "'":
            in_string = not in_string
        if not in_string:
            if char in "([":
                depth += 1
            elif char in ")]":
                depth -= 1
            elif char == "," and depth == 0:
                parts.append("".join(current))
                current = []
                continue
        current.append(char)
    if "".join(current).strip():
        parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _parameters(raw_params: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(every parameter name, the ones a caller MUST supply)``.

    Both are needed and they answer different questions. A SUBSET check uses
    the required names -- a migration that ADDS an optional parameter must not
    fail a preflight, while a missing or renamed required one must. An EXACT
    check uses every name, because PostgREST advertises defaulted parameters
    too, and the point of an exact check is that an EXTRA deployed parameter
    is a different function.
    """
    every: list[str] = []
    required: list[str] = []
    for param in _split_params(raw_params):
        # Strip a comment tail so `p_x jsonb -- default later` cannot mislead.
        text = param.split("--")[0].strip()
        if not text:
            continue
        has_default = bool(re.search(r"\bdefault\b", text, re.IGNORECASE))
        name = text.split()[0]
        if name.upper() in {"IN", "OUT", "INOUT", "VARIADIC"}:
            parts = text.split()
            if len(parts) < 2:
                continue
            name = parts[1]
        every.append(name)
        if not has_default:
            required.append(name)
    return tuple(every), tuple(required)


def migration_functions(migrations_dir: Path | str | None = None) -> dict[str, dict]:
    """Every ``public.<fn>`` the migrations define, in apply order.

    A later definition supersedes an earlier one, because that is what applying
    the migrations in order actually does.
    """
    directory = Path(migrations_dir or (REPO_ROOT / "supabase" / "migrations"))
    if not directory.is_dir():
        raise InventoryError(f"migrations directory not found: {directory}")
    files: list[tuple[tuple[int, int], Path]] = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if match is None:
            raise InventoryError(f"unsupported migration filename: {path.name!r}")
        version = match.group("version")
        files.append(((0 if len(version) == 3 else 1, int(version)), path))
    if not files:
        raise InventoryError(f"no migration files found in {directory}")
    files.sort(key=lambda item: item[0])

    provided: dict[str, dict] = {}
    for _key, path in files:
        version = _FILENAME_RE.match(path.name).group("version")  # type: ignore[union-attr]
        text = path.read_text(encoding="utf-8")
        for name, raw_params in _FUNCTION_RE.findall(text):
            every, required = _parameters(raw_params)
            key = name.lower()
            # A redefinition supersedes the earlier SIGNATURE but not the
            # earlier CREATION: `first_migration` is the migration that
            # introduces the object, which is the one whose absence is real
            # drift. `migration` is the one whose signature is deployed.
            first = provided.get(key, {}).get("first_migration", version)
            first_file = provided.get(key, {}).get("first_file", path.name)
            provided[key] = {
                "args": sorted(every),
                "required_args": sorted(required),
                "migration": version,
                "file": path.name,
                "first_migration": first,
                "first_file": first_file,
            }
    return provided


# ---------------------------------------------------------------------------
# what the runtime calls
# ---------------------------------------------------------------------------
def _callee_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def runtime_rpc_calls(repo_root: Path | str | None = None) -> dict[str, set[str]]:
    """Every RPC name this repository invokes, mapped to where it invokes it.

    Only a literal first argument counts. A dynamically built name is not
    evidence of a dependency this module can verify, and pretending otherwise
    would put a guess into a gate.
    """
    root = Path(repo_root or REPO_ROOT)
    found: dict[str, set[str]] = {}

    for relative in RUNTIME_SOURCES:
        path = root / relative
        if not path.is_file():
            raise InventoryError(f"runtime source not found: {relative}")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if _callee_name(node) not in _RPC_CALLEES:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.setdefault(first.value.lower(), set()).add(relative)

    for relative in TOOLING_SOURCES:
        path = root / relative
        if not path.is_file():
            continue
        for name in _TOOLING_RPC_RE.findall(path.read_text(encoding="utf-8")):
            found.setdefault(name.lower(), set()).add(relative)

    if not found:
        raise InventoryError("no RPC call sites were found; the scan cannot be trusted")
    return found


# ---------------------------------------------------------------------------
# the inventory
# ---------------------------------------------------------------------------
def build(repo_root: Path | str | None = None) -> dict:
    """The complete inventory, plus every reason it might not be sound."""
    root = Path(repo_root or REPO_ROOT)
    provided = migration_functions(root / "supabase" / "migrations")
    called = runtime_rpc_calls(root)

    rpcs: dict[str, dict] = {}
    problems: list[str] = []
    for name in sorted(called):
        definition = provided.get(name)
        if definition is None:
            problems.append(
                f"{name} is called at runtime ({', '.join(sorted(called[name]))}) but no "
                "migration in this repository creates it; production could never satisfy "
                "that dependency")
            continue
        rpcs[name] = {
            "args": definition["args"],
            "required_args": definition["required_args"],
            "migration": definition["migration"],
            "file": definition["file"],
            "first_migration": definition["first_migration"],
            "first_file": definition["first_file"],
            "called_by": sorted(called[name]),
        }

    migrations = sorted({entry["migration"] for entry in rpcs.values()}
                        | {entry["first_migration"] for entry in rpcs.values()},
                        key=lambda version: (0 if len(version) == 3 else 1, int(version)))
    return {
        "rpcs": rpcs,
        "migrations": migrations,
        "problems": problems,
        "ok": not problems,
    }


def required_rpc_args(repo_root: Path | str | None = None) -> dict[str, list[str]]:
    """The preflight's pinned shape: RPC name -> required argument names."""
    return {name: entry["required_args"] for name, entry in build(repo_root)["rpcs"].items()}


def missing_from_deployment(observed: dict[str, set[str] | list[str] | None], *,
                            repo_root: Path | str | None = None) -> list[str]:
    """Which required RPCs a deployment does not satisfy.

    ``observed`` maps an RPC name to the argument names the deployed database
    advertises, or ``None`` for "could not be established". Unverifiable is a
    blocking finding, never a pass: a preflight that cannot see the surface has
    not checked it.
    """
    problems: list[str] = []
    for name, entry in sorted(build(repo_root)["rpcs"].items()):
        required = set(entry["required_args"])
        if name not in observed:
            problems.append(f"{name} was not probed; the required RPC surface is unverified")
            continue
        advertised = observed[name]
        if advertised is None:
            problems.append(f"{name}: the deployed signature could not be established")
            continue
        missing = sorted(required - set(advertised))
        if missing:
            problems.append(
                f"{name}: required argument(s) {missing} are not advertised; the deployed "
                f"signature differs from {entry['file']}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Release RPC/migration inventory (read-only, offline).")
    parser.add_argument("command", choices=("rpcs", "migrations", "json", "verify"))
    parser.add_argument("--repo-root", default=None)
    args = parser.parse_args(argv)

    try:
        inventory = build(args.repo_root)
    except InventoryError as exc:
        print(f"release-inventory error: {exc}", file=sys.stderr)
        return 2

    if args.command == "rpcs":
        for name, entry in sorted(inventory["rpcs"].items()):
            print(f"{name}\t{','.join(entry['required_args'])}\t{entry['file']}")
    elif args.command == "migrations":
        for version in inventory["migrations"]:
            print(version)
    elif args.command == "json":
        print(json.dumps(inventory, indent=2, sort_keys=True))
    else:
        print(json.dumps({"ok": inventory["ok"], "rpc_count": len(inventory["rpcs"]),
                          "migration_count": len(inventory["migrations"]),
                          "problems": inventory["problems"]}, indent=2, sort_keys=True))
    return 0 if inventory["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
