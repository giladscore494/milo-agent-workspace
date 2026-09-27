"""ONE definition of what the Israeli vehicle register's own fields MEAN.

The Ministry of Transport's `degem-rechev-wltp` resource states its
meaning-bearing dimensions twice: as a numeric CODE and as the Hebrew NAME
that code is paired with (`delek_cd` 7 travels with `delek_nm`
`חשמל/בנזין`; `hanaa_cd` 3 with `hanaa_nm` `4X4`). Every table below reads
the CODE through a closed, reviewed mapping and then requires the record's own
name field to still be the name that code is paired with. A code the table does
not name, or a pairing that has drifted, is never guessed at.

Why this module exists rather than a second copy
------------------------------------------------

The R5 proof (`backend/testing/r5_proof/government.py`) established these
pairings against the committed capture, and Catalog PR2 reads the same fields
of the same resource. Two copies of a semantic table are two things that can
drift, so the pairings live here -- in the production catalog namespace, which
`backend/testing/` may import and which may never import it back -- and the R5
module now builds its tables by SELECTING the exact subset it reviewed:

    FUEL_BY_CODE = {code: _vocabulary.FUEL_BY_CODE[code] for code in (1, 7)}

That selection is deliberate and is not a formality. R5's registry tool is
CONSERVATIVE: it decodes a row and then requires the match to be unique, so
widening a table it reads would let rows it previously skipped become
candidates and could turn a settled answer into an ambiguity. Pinning R5 to
its own key set means this module can grow -- as a wider capture requires --
without moving what R5 proved, while a change to the MEANING of a shared code
breaks R5 immediately instead of quietly.

What is deliberately absent
---------------------------

There is no manufacturer table here. The register states no code for the
marque field `tozar`, so Catalog PR2 stores the marque exactly as the source
wrote it rather than transliterating it. R5 keeps its own `MAKE_BY_TOZAR`
because its job is different: it answers a request that names `Toyota` in
Latin script, which requires a reviewed alias. Storing an alias as a
manufacturer IDENTITY would be inventing a name the register never stated.

Pure module: constants and two pure predicates. No I/O, no clock, no
randomness, no global mutable state.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

#: `delek_cd` -> (fuel type, the `delek_nm` that code is paired with). The code
#: decides; the name is the cross-check that the register's own pairing still
#: holds. `7` is electricity/petrol -- a PLUG-IN hybrid, not a "hybrid".
#:
#: PR-V widened this table with the two codes the production Toyota snapshot
#: (cs1.e335295707fa8d6935b113bb6a0165f4, 6,374 rows) states with a stable
#: pairing: `2`/`דיזל` (1,097 rows) and `4`/`חשמל` (30 rows, every one with
#: propulsion code 3). A row stating no `delek_cd` at all (66 rows there) is
#: unchanged: an absent code with an absent label is simply not stated.
FUEL_BY_CODE: Mapping[int, tuple[str, str]] = {
    1: ("petrol", "בנזין"),
    2: ("diesel", "דיזל"),
    4: ("electric", "חשמל"),
    7: ("plug_in_hybrid", "חשמל/בנזין"),
}

#: `technologiat_hanaa_cd` -> (propulsion technology, its paired name).
#: PR-V: `3`/`רכב חשמלי` ("electric vehicle") -- 30 rows in the production
#: Toyota snapshot, every one fuel code 4 (`חשמל`).
PROPULSION_BY_CODE: Mapping[int, tuple[str, str]] = {
    1: ("hybrid", "היברידי רגיל"),
    2: ("plug_in", "PLUG IN"),
    3: ("battery_electric", "רכב חשמלי"),
}

#: The propulsion names the register states with NO code at all.
#:
#: `הנעה רגילה` ("regular drive") is how the resource marks a row that has no
#: coded propulsion technology, and it carries `technologiat_hanaa_cd: null`.
#: It is read here ONLY when the code field is absent, and only on an exact,
#: whole-string match against this closed table -- never on a substring and
#: never in preference to a code. A row that states a code is always read
#: through `PROPULSION_BY_CODE`; a row that states an uncoded name this table
#: does not list is left unresolved rather than interpreted.
PROPULSION_BY_UNCODED_NAME: Mapping[str, str] = {"הנעה רגילה": "conventional"}

#: `hanaa_cd` -> (drivetrain, its paired name). A row with no `hanaa_cd` (136
#: rows in the production Toyota snapshot, all with no `hanaa_nm` either) is
#: unchanged by PR-V: the dimension is simply not stated.
DRIVETRAIN_BY_CODE: Mapping[int, tuple[str, str]] = {
    1: ("two_wheel_drive", "4X2"),
    3: ("awd", "4X4"),
}

#: `merkav` (body) -> body style. The register states this one as a name only;
#: it publishes no body code, so the whole string is matched exactly.
#:
#: PR-V: the eight body names below are read from the production Toyota
#: snapshot's own distribution; their English labels are owner-approved.
#: Every other name -- and in particular the five
#: in `UNMAPPED_MERKAV_REASONS` -- stays unread: the row keeps `body_style`
#: unresolved rather than being given a guessed one.
BODY_STYLE_BY_MERKAV: Mapping[str, str] = {
    "פנאי-שטח": "suv",
    "סדאן": "sedan",
    "MPV": "mpv",
    "הצ'בק": "hatchback",
    "סטיישן": "wagon",
    "קופה": "coupe",
    "קבריולט": "convertible",
    "תא כפול": "pickup_double_cab",
    "תא בודד": "pickup_single_cab",
}

#: PR-V: the `merkav` names the production Toyota snapshot states that are
#: DELIBERATELY left unmapped, with the reviewer's reason. Read by nothing at
#: run time -- a name absent from `BODY_STYLE_BY_MERKAV` is already unresolved
#: -- and kept so the decision is written down instead of implied.
UNMAPPED_MERKAV_REASONS: Mapping[str, str] = {
    "משא אחוד":
        "'combined cargo' is a registration/usage class (a passenger-and-goods vehicle), not "
        "a body style; across Toyota it covers vans, pickups and SUVs alike (141 rows)",
    "קומבי":
        "'kombi' names a passenger/cargo configuration that the register applies to vans and "
        "estates alike; no single body style follows from it (40 rows)",
    "שדה":
        "'field' is an off-road/utility class with no reviewed body-style meaning (6 rows)",
    "ואן/נוסעים":
        "'van/passengers' is one row whose name combines two body classes; a single body "
        "style would be a guess (1 row)",
    "":
        "an empty body name states nothing; it is an absent value, read as not stated "
        "(96 rows), never as a body style",
}

#: Fuel and propulsion are two INDEPENDENT statements about one row, and a row
#: whose two statements disagree has not stated one coherent propulsion. Only
#: these pairings occur in the reviewed capture; anything else is
#: inconsistent and settles neither dimension (fails closed to unresolved).
#:
#: PR-V adds diesel with no coded propulsion (1,095 production rows) and
#: electric with a battery-electric propulsion (30). It deliberately does NOT
#: add ("diesel", "hybrid"): two production rows state it, and it stays an
#: inconsistent pair -- such a row reads with BOTH dimensions unresolved (an
#: ambiguous candidate, see `normalize.read_wltp_record`), never refused.
CONSISTENT_FUEL_PROPULSION: frozenset[tuple[str, str]] = frozenset({
    ("petrol", "hybrid"), ("petrol", "conventional"), ("plug_in_hybrid", "plug_in"),
    ("diesel", "conventional"), ("electric", "battery_electric"),
})

#: The register's OWN marker for "this row states no value for this dimension".
#: It appears as `לא ידוע קוד` and as `לא ידוע קוד 0` -- the same marker with
#: the code echoed after it. Matched WHOLE, never as a substring, so a real
#: label that merely contains those words could not be mistaken for it.
#:
#: This is materially different from a code this module does not name: here the
#: SOURCE says it has nothing to state, so the dimension is simply absent and
#: the reading is complete without it.
DECLARED_UNKNOWN_LABEL = re.compile(r"^לא ידוע קוד( [0-9]+)?$")

#: Captured fields that are deliberately NOT read as evidence, and why. The
#: reason is written by a reviewer and is never derived from the row in front
#: of the code.
UNMAPPED_FIELD_REASONS: Mapping[str, str] = {
    "koah_sus":
        "the dataset publishes no definition of this power figure, and across the captured "
        "plug-in rows of one commercial model it takes both engine-scale (177/185/186) and "
        "system-scale (302/324) values, so its semantics are unresolved in the source",
    "dg_metach_solela":
        "battery voltage is stated as 650.0, 12.0, 0.01 and null across the captured "
        "plug-in rows, which cannot all describe one traction battery",
    "mishkal_kolel":
        "a total mass is stated with no unit anywhere in the dataset or its metadata",
    "automatic_ind":
        "a 0/1 indicator with no published definition of what 0 means: across the captured "
        "rows it is 1 everywhere, so nothing establishes whether 0 would mean manual, "
        "unstated or not applicable, and a transmission cannot be read from it",
    "sug_degem":
        "a single-letter model-type marker (P/M) the dataset publishes no key for",
}

#: The record's OWN field names this module reads, in the order a reviewer
#: reads them. Closed and static: a row that grows a new field contributes
#: nothing until a reviewer adds it here.
GOVERNMENT_IDENTITY_FIELDS: tuple[str, ...] = (
    "tozar", "kinuy_mishari", "shnat_yitzur", "nefah_manoa", "delek_cd", "delek_nm",
    "technologiat_hanaa_cd", "technologiat_hanaa_nm", "degem_nm", "hanaa_cd",
    "hanaa_nm", "merkav", "ramat_gimur",
)

#: The register's own row identity, as the CKAN datastore serves it.
GOVERNMENT_RECORD_ID_FIELD = "_id"

#: The narrowest and widest model year this catalog will read from a register
#: row. Mirrors the durable `catalog_candidate_variants` year constraint, so a
#: year the database would refuse is refused before a write is attempted.
MIN_MODEL_YEAR = 1900
MAX_MODEL_YEAR = 2100


def unmapped_fields(*fields: str) -> tuple[tuple[str, str], ...]:
    """The `(field, reason)` pairs for an explicit, ordered selection.

    Callers name the fields they are reporting, so a reader of the call site
    sees exactly which gaps travel with a result -- and a field this module
    does not explain fails closed here rather than being reported with an
    invented reason.
    """
    try:
        return tuple((field, UNMAPPED_FIELD_REASONS[field]) for field in fields)
    except KeyError:
        raise ValueError("no reviewed reason explains this unmapped field") from None


def is_declared_unknown(label: Any) -> bool:
    """Whether the register itself declared this dimension to have no value."""
    return isinstance(label, str) and DECLARED_UNKNOWN_LABEL.fullmatch(label.strip()) is not None


__all__ = ["BODY_STYLE_BY_MERKAV", "CONSISTENT_FUEL_PROPULSION", "DECLARED_UNKNOWN_LABEL",
           "DRIVETRAIN_BY_CODE", "FUEL_BY_CODE", "GOVERNMENT_IDENTITY_FIELDS",
           "GOVERNMENT_RECORD_ID_FIELD", "MAX_MODEL_YEAR", "MIN_MODEL_YEAR",
           "PROPULSION_BY_CODE", "PROPULSION_BY_UNCODED_NAME", "UNMAPPED_FIELD_REASONS",
           "UNMAPPED_MERKAV_REASONS",
           "is_declared_unknown", "unmapped_fields"]
