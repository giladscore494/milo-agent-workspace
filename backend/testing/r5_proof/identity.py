"""R5: the ONE canonical vehicle entity key every proof mapper shares.

R4 already owns reconciliation identity: `comparison.scope_identity` is the
canonical scope (entity, field, geography, market, time scope) plus the closed
identity dimensions. This module deliberately does NOT add a second identity
contract. It contributes exactly one thing R4 leaves to the caller -- what the
`entity` string of a vehicle is -- so three independently written mappers name
the same vehicle the same way instead of each inventing a spelling.

Everything that NARROWS identity beyond the entity stays where R4 put it: in
`StructuredEvidenceFact.identity`, from the closed `IDENTITY_DIMENSIONS`
vocabulary, stated only when a source actually states it.

Pure module: no I/O, no state, no model.
"""

from __future__ import annotations

import re

from backend.engines.swarm_v2.normalization import normalize_field_key

_ENTITY_SAFE = re.compile(r"[^a-z0-9]+")

#: The market identifiers this proof recognises. A market is part of the R4
#: comparison scope, so an unrecognised one must fail closed rather than widen
#: a comparison: evidence about one market never verifies a claim about another.
KNOWN_MARKETS = frozenset({"IL"})


class VehicleIdentityError(ValueError):
    """A vehicle identity that cannot be stated conservatively."""


def _segment(value: str) -> str:
    """One normalized, bounded entity segment, or fail closed."""
    text = _ENTITY_SAFE.sub("-", normalize_field_key(str(value))).strip("-")
    if not text or len(text) > 60:
        raise VehicleIdentityError("a vehicle entity segment must be short and non-empty")
    return text


def vehicle_entity_key(*, make: str, commercial_model: str, market: str) -> str:
    """The canonical entity of ONE commercial vehicle model in ONE market.

    Deliberately make + commercial model + market, and nothing more. It is the
    ENTITY, not the variant: the whole point of R4's identity dimensions is
    that a generation, an engine, a transmission or an official code narrows
    this further, so folding any of them into the entity string would hide the
    narrowing R4 exists to perform. Two sources describing the same commercial
    model in the same market therefore agree on the entity and are then
    separated -- or not -- by the dimensions they each actually state.
    """
    if str(market).strip().upper() not in KNOWN_MARKETS:
        raise VehicleIdentityError("unrecognised market")
    return f"{_segment(make)}:{_segment(commercial_model)}:{str(market).strip().lower()}"


__all__ = ["KNOWN_MARKETS", "VehicleIdentityError", "vehicle_entity_key"]
