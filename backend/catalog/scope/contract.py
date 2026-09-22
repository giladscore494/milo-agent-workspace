"""The ONE canonical WorkScope: what a mapping plan intends, and nothing else.

What a WorkScope says
---------------------

Which marques to map and in what order, which model years, how many
candidates at most, and how many candidates one batch run may take:

    {"batch_size": 10,
     "contract": "milo-work-scope/1",
     "directory_version": "milo-manufacturer-directory/1",
     "max_items": 800,
     "model_years": {"from": 2018, "to": null},
     "source": {"family": "government", "package_id": "degem-rechev-wltp",
                "resource_id": "142afde2-6228-49f9-8a29-9b6c3a0cbe40"},
     "units": ["toyota", "lexus"]}

`units` is ordered: its order IS the priority, so "Toyota first, then Mazda"
and a list the user reordered by hand are the same fact stated the same way.

Two inputs, one contract
------------------------

A typed instruction (`interpret.py`) and an edit made in the Mapping Plan are
both only INPUTS. Each is reduced to the same four fields and handed to
`scope_from_fields` below, which is the only constructor of a `WorkScope`. So
the two cannot drift into two scopes: they produce the same canonical record,
the same canonical text and therefore the same digest, or one of them is
refused.

Where the digest comes from
---------------------------

`canonical_text` renders the record with sorted keys, compact separators and
ASCII escaping -- every value in it is ASCII anyway -- and the digest is the
SHA-256 of exactly that text. The database stores the TEXT and derives the
digest from it itself (a CHECK constraint: `digest = sha256(scope_text)`), so
the stored digest cannot disagree with the stored plan by any write path, and
the in-memory mirror hashes the same text. Unlike the storage-local raw-record
digest (`backend/catalog/digest.py`), this one is portable ON PURPOSE: it
travels to the browser and back, and a batch launch will be refused unless the
browser names the exact digest it was shown.

What is deliberately not in it
------------------------------

No Government version and no snapshot: those are facts about a PREPARATION,
decided when the register is read, and a plan made today must not pretend to
know what the register will say then. No model filter: the reviewed first
contract selects whole marques by model year. And no browser-supplied text:
the record is built from validated scalars and directory keys only.

Pure module. No I/O, no clock, no environment.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from backend.catalog.government import source as src
from backend.catalog.government import vocabulary as vocab

from . import directory as mdir

#: The contract a stored plan was built under. Changing any rule below that
#: affects what a stored plan MEANS changes this string.
WORK_SCOPE_CONTRACT = "milo-work-scope/1"

#: Where every unit of this contract is read from. Constant for `/1`, and still
#: part of the record, so the digest commits to the source and a later
#: contract that reads another resource cannot be confused with this one.
WORK_SCOPE_SOURCE: Mapping[str, str] = {
    "family": src.GOVERNMENT_SOURCE_FAMILY,
    "package_id": src.CKAN_PACKAGE_ID,
    "resource_id": src.WLTP_RESOURCE_ID,
}

#: The hard server maximum for one batch. A batch is one product run, and the
#: reviewed per-run envelope (`backend/runtime_policy.py`: 23 tasks, 56 agent
#: steps) admits 20 candidates with room for the Commander and the correction
#: round -- `plan_worst_case(20)` is 47 agent steps. Not configurable.
MAX_BATCH_SIZE = 20

#: The batch size a plan takes when nobody chose one. The first paid website
#: run is one batch at this size.
DEFAULT_BATCH_SIZE = 10

#: The most candidates one plan may cover, across all of its units. It bounds
#: the durable queue a plan can ever materialize.
MAX_WORK_SCOPE_ITEMS = 2000

#: The limit a NEW plan takes when its instruction states none. Always
#: reported back as a note, so a default is never mistaken for a request.
DEFAULT_MAX_ITEMS = 100

#: The model years a plan may name: the same bounds the register reading
#: applies, so a plan cannot ask for a year no candidate can carry.
MIN_MODEL_YEAR = vocab.MIN_MODEL_YEAR
MAX_MODEL_YEAR = vocab.MAX_MODEL_YEAR

#: The most units one plan may name: every directory entry, once.
MAX_UNITS = mdir.MAX_DIRECTORY_ENTRIES

#: The bound on the canonical text. The largest valid record (every entry,
#: both years, the maximum limit) is well under it; the database applies the
#: same number so an oversized record is refused by both.
MAX_SCOPE_TEXT_CHARS = 4000

#: The closed key set of a stored record, and of its two nested objects.
RECORD_KEYS = frozenset({"batch_size", "contract", "directory_version", "max_items",
                         "model_years", "source", "units"})
MODEL_YEAR_KEYS = frozenset({"from", "to"})

#: The closed key set of an EDIT: exactly the four editable facts, the years as
#: two keys. An edit cannot name a contract, a source, a directory version or a
#: digest -- those are the server's.
FIELD_KEYS = frozenset({"units", "model_year_from", "model_year_to", "max_items", "batch_size"})

#: The closed vocabulary of refusals. Each names the FIELD that failed and
#: carries no value, so a message can be shown and logged without echoing what
#: was sent. `frontend/lib/errorText.ts` authors the copy a person reads.
WORK_SCOPE_REASONS: Mapping[str, str] = {
    "WORK_SCOPE_FIELDS_INVALID":
        "an edit states exactly the units, both model-year bounds, the limit and the batch size",
    "WORK_SCOPE_UNITS_INVALID":
        "a plan names one to all of the directory's manufacturers, each once",
    "WORK_SCOPE_YEARS_INVALID":
        "the model years must be whole years in range, the first not after the last",
    "WORK_SCOPE_MAX_ITEMS_INVALID":
        f"the candidate limit must be a whole number from 1 to {MAX_WORK_SCOPE_ITEMS}",
    "WORK_SCOPE_BATCH_SIZE_INVALID":
        f"the batch size must be a whole number from 1 to {MAX_BATCH_SIZE}",
    "WORK_SCOPE_RECORD_INVALID": "the stored plan is not a valid work scope",
}


class WorkScopeError(ValueError):
    """A refusal carrying ONLY a static, code-owned message."""

    def __init__(self, code: str):
        if code not in WORK_SCOPE_REASONS:
            raise ValueError("work scope refusal must come from the static allowlist")
        self.code = code
        self.safe_message = WORK_SCOPE_REASONS[code]
        super().__init__(self.safe_message)


@dataclass(frozen=True)
class WorkScope:
    """One validated plan. Construct it through `scope_from_fields` only."""

    units: tuple[str, ...]
    model_year_from: int | None
    model_year_to: int | None
    max_items: int
    batch_size: int
    directory_version: str = mdir.DIRECTORY_VERSION

    def as_record(self) -> dict[str, Any]:
        return {
            "batch_size": self.batch_size,
            "contract": WORK_SCOPE_CONTRACT,
            "directory_version": self.directory_version,
            "max_items": self.max_items,
            "model_years": {"from": self.model_year_from, "to": self.model_year_to},
            "source": dict(WORK_SCOPE_SOURCE),
            "units": list(self.units),
        }

    def canonical_text(self) -> str:
        return canonical_text(self.as_record())

    def digest(self) -> str:
        return scope_digest(self.canonical_text())

    @property
    def current(self) -> bool:
        """Whether this plan was validated against the directory in force now."""
        return self.directory_version == mdir.DIRECTORY_VERSION

    def fields(self) -> dict[str, Any]:
        """The four editable fields, as `scope_from_fields` accepts them."""
        return {"units": list(self.units), "model_year_from": self.model_year_from,
                "model_year_to": self.model_year_to, "max_items": self.max_items,
                "batch_size": self.batch_size}


def canonical_text(record: Mapping[str, Any]) -> str:
    """The ONE rendering a digest is taken over. See the module docstring."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def scope_digest(text: str) -> str:
    """SHA-256 of the canonical text, lowercase hex -- exactly what the
    database derives with `encode(sha256(convert_to(text, 'UTF8')), 'hex')`."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _units(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= MAX_UNITS:
        raise WorkScopeError("WORK_SCOPE_UNITS_INVALID")
    if any(mdir.entry_for(key) is None for key in value) or len(set(value)) != len(value):
        raise WorkScopeError("WORK_SCOPE_UNITS_INVALID")
    return tuple(value)


def _year(value: Any) -> int | None:
    if value is None:
        return None
    if not _whole(value) or not MIN_MODEL_YEAR <= value <= MAX_MODEL_YEAR:
        raise WorkScopeError("WORK_SCOPE_YEARS_INVALID")
    return value


def scope_from_fields(fields: Mapping[str, Any]) -> WorkScope:
    """The only constructor of a `WorkScope`, for chat and UI alike.

    The key set is closed -- a field this contract does not name is refused,
    not ignored -- and every field is checked for TYPE as well as range: a
    `True` is not a batch size and `"10"` is not a limit, whichever input
    produced them. Nothing is
    clamped, trimmed, sorted or de-duplicated -- a value outside the contract
    is a refusal naming its field, because a quietly repaired plan is a plan
    nobody asked for.
    """
    if not isinstance(fields, Mapping) or set(fields) != FIELD_KEYS:
        raise WorkScopeError("WORK_SCOPE_FIELDS_INVALID")
    units = _units(fields.get("units"))
    year_from = _year(fields.get("model_year_from"))
    year_to = _year(fields.get("model_year_to"))
    if year_from is not None and year_to is not None and year_from > year_to:
        raise WorkScopeError("WORK_SCOPE_YEARS_INVALID")
    max_items = fields.get("max_items")
    if not _whole(max_items) or not 1 <= max_items <= MAX_WORK_SCOPE_ITEMS:
        raise WorkScopeError("WORK_SCOPE_MAX_ITEMS_INVALID")
    batch_size = fields.get("batch_size")
    if not _whole(batch_size) or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise WorkScopeError("WORK_SCOPE_BATCH_SIZE_INVALID")
    return WorkScope(units=units, model_year_from=year_from, model_year_to=year_to,
                     max_items=max_items, batch_size=batch_size)


#: A unit key's SHAPE, as the database checks it. Which keys EXIST is the
#: reviewed directory's business, not SQL's.
_UNIT_KEY = re.compile(r"[a-z][a-z0-9_]{0,39}")

#: The most units the database admits in one record. The directory is smaller;
#: this is the durable ceiling a directory may grow into.
MAX_STORED_UNITS = 64


def stored_record_valid(record: Any) -> bool:
    """The Python twin of `public.catalog_work_scope_record_valid`.

    SHAPE and hard bounds only, exactly as the database checks them, so the
    in-memory repository refuses precisely what PostgreSQL refuses. It is
    deliberately weaker than `scope_from_text`, which also requires every key
    to be a directory entry and the text to be canonical: the database cannot
    know the directory, and does not pretend to.
    """
    if not isinstance(record, dict) or set(record) != RECORD_KEYS:
        return False
    if record["contract"] != WORK_SCOPE_CONTRACT or record["source"] != dict(WORK_SCOPE_SOURCE):
        return False
    version = record["directory_version"]
    if not isinstance(version, str) or not 1 <= len(version) <= 80:
        return False
    if not _whole(record["batch_size"]) or not 1 <= record["batch_size"] <= MAX_BATCH_SIZE:
        return False
    if not _whole(record["max_items"]) or not 1 <= record["max_items"] <= MAX_WORK_SCOPE_ITEMS:
        return False
    units = record["units"]
    if not isinstance(units, list) or not 1 <= len(units) <= MAX_STORED_UNITS \
            or any(not isinstance(key, str) or not _UNIT_KEY.fullmatch(key) for key in units) \
            or len(set(units)) != len(units):
        return False
    years = record["model_years"]
    if not isinstance(years, dict) or set(years) != MODEL_YEAR_KEYS:
        return False
    for value in years.values():
        if value is not None and (not _whole(value) or not 1900 <= value <= 2100):
            return False
    return not (years["from"] is not None and years["to"] is not None
                and years["from"] > years["to"])


def scope_from_text(text: Any) -> WorkScope:
    """Re-read a STORED plan, strictly.

    The stored text must parse to a record with exactly the closed key set,
    this contract, this source, and fields `scope_from_fields` accepts -- and
    it must be the canonical rendering of that record, byte for byte, so the
    digest the database derived from it is the digest of this plan and of no
    other rendering. A record validated against an older directory still
    reads (it is history); `WorkScope.current` says it is not current, and its
    keys are re-checked against the directory in force, because a key that no
    longer exists cannot be displayed as a marque it no longer names.
    """
    if not isinstance(text, str) or not 0 < len(text) <= MAX_SCOPE_TEXT_CHARS:
        raise WorkScopeError("WORK_SCOPE_RECORD_INVALID")
    try:
        record = json.loads(text)
    except ValueError:
        raise WorkScopeError("WORK_SCOPE_RECORD_INVALID") from None
    if not isinstance(record, dict) or set(record) != RECORD_KEYS:
        raise WorkScopeError("WORK_SCOPE_RECORD_INVALID")
    if record["contract"] != WORK_SCOPE_CONTRACT or record["source"] != dict(WORK_SCOPE_SOURCE):
        raise WorkScopeError("WORK_SCOPE_RECORD_INVALID")
    years = record["model_years"]
    version = record["directory_version"]
    if not isinstance(years, dict) or set(years) != MODEL_YEAR_KEYS \
            or not isinstance(version, str) or not version:
        raise WorkScopeError("WORK_SCOPE_RECORD_INVALID")
    try:
        scope = scope_from_fields({"units": record["units"], "model_year_from": years["from"],
                                   "model_year_to": years["to"],
                                   "max_items": record["max_items"],
                                   "batch_size": record["batch_size"]})
    except WorkScopeError:
        raise WorkScopeError("WORK_SCOPE_RECORD_INVALID") from None
    scope = WorkScope(units=scope.units, model_year_from=scope.model_year_from,
                      model_year_to=scope.model_year_to, max_items=scope.max_items,
                      batch_size=scope.batch_size, directory_version=version)
    if scope.canonical_text() != text:
        raise WorkScopeError("WORK_SCOPE_RECORD_INVALID")
    return scope


__all__ = ["DEFAULT_BATCH_SIZE", "DEFAULT_MAX_ITEMS", "FIELD_KEYS", "MAX_BATCH_SIZE",
           "MAX_MODEL_YEAR", "MAX_SCOPE_TEXT_CHARS", "MAX_STORED_UNITS", "MAX_UNITS",
           "MAX_WORK_SCOPE_ITEMS", "MIN_MODEL_YEAR", "MODEL_YEAR_KEYS", "RECORD_KEYS",
           "WORK_SCOPE_CONTRACT", "WORK_SCOPE_REASONS", "WORK_SCOPE_SOURCE", "WorkScope",
           "WorkScopeError", "canonical_text", "scope_digest", "scope_from_fields",
           "scope_from_text", "stored_record_valid"]
