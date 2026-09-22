"""The ONE trusted execution scope of a Vehicle Catalog V1 run.

The defect this module exists to close
--------------------------------------

`VehicleCatalogV1Adapter.run` built its configuration from TOP-LEVEL
`run.input` keys -- `manufacturer` (or `make`), `market` and `period` -- and
fell back to the engine defaults when they were absent. No run creator in this
repository ever writes those keys: the atomic creator
(`create_message_and_run_v3`, migration `20260921000200`) persists
`input = {message_id, content, metadata}` and nothing else. Every
website-created V1 run therefore executed the ENGINE DEFAULTS -- Hyundai,
Israel, "2010 to June 2026" -- whatever project it belonged to, and nothing
anywhere said so.

The trusted contract
--------------------

A V1 project already states the scope it maps, in its SERVER-OWNED
`projects.configuration`, and has done since the project was seeded
(`001_project_workspace.sql`)::

    {"manufacturer": "Hyundai", "market": "Israel",
     "period": {"from": "2010", "to": "June 2026"}}

Nothing read it. This module makes that the one source of a V1 run's scope:

*   **Resolved at run creation, from the trusted relation.** The API reads the
    run's project the same way it reads the project's `workflow_key` for the
    immutable RunIdentity -- never from the request body.
*   **Bound into the run in the creation transaction.** The validated scope is
    written into the run's input under `SCOPE_METADATA_KEY` by the API, inside
    the same atomic V3 insert as the message, the run and its identity. A
    request that tries to SUPPLY that key is refused before anything is
    written, so a value stored there can only have come from the server.
*   **Immutable afterwards.** A later edit of the project configuration does not
    change what an existing run maps; an idempotent replay returns the run as
    it was created, scope included.
*   **Re-validated by the worker**, which hands the scope to the adapter
    explicitly. The adapter no longer reads any scope from run input and no
    longer defaults: a run without a bound scope is refused, not quietly
    turned into a Hyundai run.

What this deliberately does NOT do
----------------------------------

*   It does not parse the user's free text. `content` is the task description;
    it is not, and never was, the reviewed V1 execution contract.
*   It does not change the engine. `VehicleCatalogRunConfig` keeps its defaults
    for direct, non-website callers of `VehicleCatalogEngine`, exactly as the
    preserved pipeline requires. For the canonical seeded project the resolved
    configuration is byte-for-byte the previous default configuration, so its
    prompts do not change at all.
*   It does not trust request metadata. The browser cannot name a scope.

Pure module: dict reading and string checks. No I/O, no clock, no randomness,
and nothing from `backend` is imported, so the API can use it without pulling
the V1 engine into its process.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

#: Where the validated scope is bound inside a run's `input.metadata`. RESERVED:
#: only the API writes it, and a request that supplies it is refused.
SCOPE_METADATA_KEY = "vehicle_catalog_scope"

#: The bound record's own schema version, so a reader refuses a shape it does
#: not know instead of guessing at it.
SCOPE_RECORD_VERSION = "milo-vehicle-catalog-scope/1"

#: Where the bound scope was resolved from. There is exactly one source.
SCOPE_SOURCE = "project_configuration"

#: The configuration keys that together state a V1 scope.
SCOPE_CONFIGURATION_KEYS: tuple[str, ...] = ("manufacturer", "market", "period")

#: Bounds on each stated value. Every value reaches a model prompt, so it is
#: bounded here rather than trusted to be short.
MAX_MANUFACTURER_CHARS = 80
MAX_MARKET_CHARS = 60
MAX_PERIOD_BOUND_CHARS = 40

#: The closed refusal vocabulary. Each names the PROPERTY that failed and never
#: quotes the value, so a message is safe for an API body, a run error and an
#: event alike.
SCOPE_REASONS: Mapping[str, str] = {
    "VEHICLE_CATALOG_SCOPE_NOT_CONFIGURED":
        "this vehicle catalog project does not configure a manufacturer, market and period",
    "VEHICLE_CATALOG_SCOPE_INVALID":
        "this vehicle catalog project's configured scope is not a valid manufacturer, market and period",
    "VEHICLE_CATALOG_SCOPE_MISSING":
        "this vehicle catalog run carries no bound scope, so it cannot be executed",
    "VEHICLE_CATALOG_SCOPE_RESERVED":
        "the vehicle catalog scope is resolved by the server and cannot be supplied with a request",
}


class VehicleCatalogScopeError(ValueError):
    """A refusal carrying ONLY a static, code-owned reason."""

    def __init__(self, code: str) -> None:
        if code not in SCOPE_REASONS:
            raise ValueError("vehicle catalog scope refusal must come from the static allowlist")
        self.code = code
        self.safe_message = SCOPE_REASONS[code]
        super().__init__(self.safe_message)


@dataclass(frozen=True)
class VehicleCatalogScope:
    """What ONE V1 run maps: a manufacturer, a market and a period."""

    manufacturer: str
    market: str
    period_from: str
    period_to: str

    @property
    def period(self) -> str:
        """The engine's period text.

        `"2010" .. "June 2026"` renders as `"2010 to June 2026"`, which is
        exactly `core.DEFAULT_PERIOD`: the canonical seeded project resolves to
        the configuration the engine always ran with, so its prompts are
        unchanged.
        """
        return f"{self.period_from} to {self.period_to}"

    def as_record(self) -> dict[str, Any]:
        """The bound record, stored under `SCOPE_METADATA_KEY`."""
        return {
            "version": SCOPE_RECORD_VERSION,
            "source": SCOPE_SOURCE,
            "manufacturer": self.manufacturer,
            "market": self.market,
            "period": {"from": self.period_from, "to": self.period_to},
        }


def _exact_text(value: Any, limit: int, code: str) -> str:
    """A stated value, exactly as written, or the refusal `code`.

    Exact means exact: not a string, empty, padded, over the bound or holding a
    control character are all refusals rather than repairs. The configuration
    is server-owned, so a value that needs repairing is a value nobody reviewed.
    """
    if not isinstance(value, str) or not value or value.strip() != value:
        raise VehicleCatalogScopeError(code)
    if len(value) > limit or not value.isprintable():
        raise VehicleCatalogScopeError(code)
    return value


def _period(value: Any, code: str) -> tuple[str, str]:
    """`{"from": ..., "to": ...}` and nothing else, or the refusal `code`."""
    if not isinstance(value, Mapping) or set(value) != {"from", "to"}:
        raise VehicleCatalogScopeError(code)
    return (_exact_text(value.get("from"), MAX_PERIOD_BOUND_CHARS, code),
            _exact_text(value.get("to"), MAX_PERIOD_BOUND_CHARS, code))


def scope_from_project_configuration(configuration: Any) -> VehicleCatalogScope:
    """The scope a V1 project's configuration states, or a static refusal.

    A configuration that names NONE of the scope keys has not configured a
    scope (`..._NOT_CONFIGURED`); one that names some of them, or names them
    with an unusable value, has configured it wrongly (`..._INVALID`). Keys
    outside the scope -- a smoke project's `stage`, say -- are not read.
    """
    if not isinstance(configuration, Mapping):
        raise VehicleCatalogScopeError("VEHICLE_CATALOG_SCOPE_NOT_CONFIGURED")
    stated = [key for key in SCOPE_CONFIGURATION_KEYS if key in configuration]
    if not stated:
        raise VehicleCatalogScopeError("VEHICLE_CATALOG_SCOPE_NOT_CONFIGURED")
    code = "VEHICLE_CATALOG_SCOPE_INVALID"
    if len(stated) != len(SCOPE_CONFIGURATION_KEYS):
        raise VehicleCatalogScopeError(code)
    period_from, period_to = _period(configuration["period"], code)
    return VehicleCatalogScope(
        manufacturer=_exact_text(configuration["manufacturer"], MAX_MANUFACTURER_CHARS, code),
        market=_exact_text(configuration["market"], MAX_MARKET_CHARS, code),
        period_from=period_from, period_to=period_to)


def scope_from_record(record: Any) -> VehicleCatalogScope:
    """Read a BOUND record back, or refuse it.

    Closed in both directions: the version, the source and the key set must be
    exactly the ones `as_record` writes, so a record this release did not write
    is refused rather than interpreted.
    """
    code = "VEHICLE_CATALOG_SCOPE_INVALID"
    if not isinstance(record, Mapping):
        raise VehicleCatalogScopeError(code)
    if set(record) != {"version", "source", "manufacturer", "market", "period"}:
        raise VehicleCatalogScopeError(code)
    if record.get("version") != SCOPE_RECORD_VERSION or record.get("source") != SCOPE_SOURCE:
        raise VehicleCatalogScopeError(code)
    period_from, period_to = _period(record["period"], code)
    return VehicleCatalogScope(
        manufacturer=_exact_text(record["manufacturer"], MAX_MANUFACTURER_CHARS, code),
        market=_exact_text(record["market"], MAX_MARKET_CHARS, code),
        period_from=period_from, period_to=period_to)


def scope_from_run(run: Any) -> VehicleCatalogScope:
    """The scope the server bound into a run at creation, or a refusal.

    `..._MISSING` when the run carries none -- which, for a run created by this
    release's API, is impossible -- and `..._INVALID` when it carries one this
    release cannot read.
    """
    run_input = run.get("input") if isinstance(run, Mapping) else None
    metadata = run_input.get("metadata") if isinstance(run_input, Mapping) else None
    if not isinstance(metadata, Mapping) or SCOPE_METADATA_KEY not in metadata:
        raise VehicleCatalogScopeError("VEHICLE_CATALOG_SCOPE_MISSING")
    return scope_from_record(metadata[SCOPE_METADATA_KEY])


def refuse_supplied_scope(metadata: Any) -> None:
    """Refuse a request that tries to name the scope itself.

    The key is the server's. Accepting a supplied value -- or silently
    overwriting it -- would let the request fingerprint describe a scope the
    run does not have, so the request is refused before anything is read or
    written.
    """
    if isinstance(metadata, Mapping) and SCOPE_METADATA_KEY in metadata:
        raise VehicleCatalogScopeError("VEHICLE_CATALOG_SCOPE_RESERVED")


__all__ = ["MAX_MANUFACTURER_CHARS", "MAX_MARKET_CHARS", "MAX_PERIOD_BOUND_CHARS",
           "SCOPE_CONFIGURATION_KEYS", "SCOPE_METADATA_KEY", "SCOPE_REASONS",
           "SCOPE_RECORD_VERSION", "SCOPE_SOURCE", "VehicleCatalogScope",
           "VehicleCatalogScopeError", "refuse_supplied_scope", "scope_from_project_configuration",
           "scope_from_record", "scope_from_run"]
