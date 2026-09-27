"""The Government register's own registration identifiers, as ONE rendering.

`tozeret_cd` (manufacturer code), `degem_cd` (model code) and `sug_degem`
(vehicle type code), read VERBATIM from a stored raw register payload exactly
as PostgreSQL's ``payload->>'field'`` renders them -- no normalization. The
variant identity key (`coverage.variant_identity_key`, PR-Z2), the bounded
variant read's filters (`catalog_candidate_variant_page`, PR-Z3) and the
identity projection all use this one rendering, so a code read from one of
them is exactly what the others compare.

A leaf module: it imports nothing from the catalog, so the coverage ledger and
the Government query layer can both depend on it.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, Mapping

#: The Government registration identifiers the key carries (contract /2), in
#: key order, as (name, raw payload field): read verbatim from the stored raw
#: record, never from the candidate's normalized columns.
REGISTER_IDENTITY_FIELDS = (("register_manufacturer_code", "tozeret_cd"),
                            ("register_model_code", "degem_cd"),
                            ("vehicle_type_code", "sug_degem"))


def register_code(payload: Mapping[str, Any], name: str) -> str | None:
    """``payload->>name`` exactly as PostgreSQL renders it: verbatim, no normalization.

    A string as it is, a whole number in decimal, a boolean as ``true`` /
    ``false``, a fraction in plain (never exponent) notation; absent or JSON
    null is None. A nested object or array names no register code and is
    refused rather than rendered differently from the database.
    """
    value = payload.get(name)
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return format(Decimal(repr(value)), "f")
    raise ValueError("a register identity code is a JSON scalar")


def register_codes(payload: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    """(manufacturer code, model code, vehicle type code) of one raw payload."""
    manufacturer, model, vehicle_type = (register_code(payload, field)
                                         for _name, field in REGISTER_IDENTITY_FIELDS)
    return manufacturer, model, vehicle_type


__all__ = ["REGISTER_IDENTITY_FIELDS", "register_code", "register_codes"]
