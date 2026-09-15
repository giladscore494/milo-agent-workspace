"""One register row -> one candidate identity, deterministically or not at all.

The rules, in the order they bind
---------------------------------

1.  **A code decides; its label is the cross-check.** Every meaning-bearing
    dimension of this resource is published twice -- as a numeric code and as
    the Hebrew name that code is paired with. The code is read through a closed
    reviewed table (`vocabulary.py`) and the row's own name field must still be
    the name that code is paired with. Nothing is read from a substring, and
    nothing is read from a label while a code exists.

2.  **A contradiction is a refusal, not a preference.** A code this vocabulary
    names, travelling with a label it is not paired with, means the register
    and this reading disagree about what the code MEANS. There is no
    conservative way to pick a side, so the row is refused and reported --
    never silently read through the code and never through the label.

3.  **The register's own "no value" marker is an ABSENCE.** `לא ידוע קוד`
    ("unknown code") is the register stating it has nothing to say about that
    dimension. The dimension is then simply absent -- an absent key, never an
    empty string and never `"unknown"` -- and the reading is COMPLETE without
    it, because nothing was left unread.

4.  **A code this vocabulary does not name leaves the reading UNSETTLED.**
    Here the register did state something and this code could not read it. The
    dimension is left unstated -- guessing is what rule 1 exists to prevent --
    and the candidate is recorded with status `ambiguous`, which in the Catalog
    PR1 vocabulary is a first-class answer rather than a staging state. The
    dimensions that could not be settled travel with the reading, so the gap is
    visible rather than inferable from an absence.

5.  **Identity text is the register's own.** The marque (`tozar`), the
    commercial model (`kinuy_mishari`), the official model code (`degem_nm`)
    and the trim (`ramat_gimur`) are stored exactly as the register wrote them.
    The register publishes no code for the marque, so transliterating it would
    be inventing a name it never stated; and two commercial models it spells
    differently -- `RAV4`, `RAV4 HYBRID`, `RAV4 PLUG-IN` -- stay different,
    because SIMILARITY IS NOT IDENTITY and nothing here merges on it.

6.  **No row-level market.** The Israeli scope belongs to the SOURCE: the
    publisher is the Ministry of Transport and the dataset is the register of
    models approved for the Israeli market. No row states a market, so none is
    invented on one; the scope is recorded once, on the snapshot.

7.  **One row is one candidate.** A model year with several trims produces
    several candidates, one per register row, because that is what the register
    states. Nothing here picks a first row, merges two rows, or collapses a
    multi-variant year into one identity.

What is read and NOT turned into identity
-----------------------------------------

`nefah_manoa` -- the homologated displacement in cubic centimetres -- is read
and validated here, but the closed candidate identity vocabulary has no
displacement dimension and inventing one would be a schema change made in a
normalizer. It travels on the reading, is derivable again from the preserved
raw payload, and reaches a consumer through the query projection.

`koah_sus`, `dg_metach_solela`, `mishkal_kolel`, `automatic_ind` and
`sug_degem` are captured in the raw record and deliberately not read at all;
each carries a reviewer's reason in `vocabulary.UNMAPPED_FIELD_REASONS`.
`koah_sus` in particular is NOT mapped to horsepower: across the captured rows
of one commercial model it takes both engine-scale and system-scale values, so
its semantics are unresolved IN THE SOURCE.

Pure module: dict reading and string checks. No I/O, no clock, no randomness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from backend.catalog.contracts import CANDIDATE_IDENTITY_DIMENSIONS

from . import source as src
from . import vocabulary as vocab

#: The bounds the durable candidate columns impose, applied HERE so an
#: over-long identity is a reported refusal rather than a database error in the
#: middle of an ingestion.
MAX_MANUFACTURER_CHARS = 120
MAX_COMMERCIAL_MODEL_CHARS = 200
MAX_MODEL_CODE_CHARS = 120
MAX_TRIM_CHARS = 120

#: The closed vocabulary of per-row normalization refusals. A refusal names the
#: PROPERTY that failed and carries no field value and no row.
GOVERNMENT_NORMALIZATION_REASONS: Mapping[str, str] = {
    "GOV_NORM_RESOURCE_UNSUPPORTED":
        "no reviewed identity normalization exists for that government resource",
    "GOV_NORM_RECORD_SHAPE_INVALID": "a register row is not an object",
    "GOV_NORM_MANUFACTURER_MISSING": "a register row states no marque",
    "GOV_NORM_MODEL_MISSING": "a register row states no commercial model",
    "GOV_NORM_MODEL_YEAR_INVALID": "a register row states no usable model year",
    "GOV_NORM_IDENTITY_TOO_LONG": "a register row states an identity beyond the durable bound",
    "GOV_NORM_LABEL_CONTRADICTION":
        "a register row pairs a known code with a label that code is not paired with",
    "GOV_NORM_FUEL_PROPULSION_CONTRADICTION":
        "a register row states a fuel and a propulsion technology that cannot both hold",
}


class GovernmentNormalizationError(ValueError):
    """A per-row refusal carrying ONLY a static, code-owned reason."""

    def __init__(self, reason_code: str):
        if reason_code not in GOVERNMENT_NORMALIZATION_REASONS:
            raise ValueError("government normalization reason must come from the static allowlist")
        self.reason_code = reason_code
        self.safe_message = GOVERNMENT_NORMALIZATION_REASONS[reason_code]
        super().__init__(self.safe_message)


@dataclass(frozen=True)
class RecordReading:
    """What one register row deterministically says about one vehicle."""

    upstream_record_id: str
    manufacturer: str
    commercial_model: str
    model_year_start: int
    model_year_end: int
    official_model_code: str | None
    trim: str | None
    identity_dimensions: dict[str, str] = field(default_factory=dict)
    #: Dimensions the register STATED and this vocabulary could not settle.
    #: Non-empty is exactly what makes the candidate `ambiguous`.
    unresolved_dimensions: tuple[str, ...] = ()
    #: Read and validated, but not part of the closed candidate identity.
    engine_displacement_cc: int | None = None

    @property
    def status(self) -> str:
        """`ambiguous` when the register said something this reading could not
        settle; `candidate` when the reading is complete."""
        return "ambiguous" if self.unresolved_dimensions else "candidate"

    def candidate_payload(self, record_row: Mapping[str, Any]) -> dict[str, Any]:
        """The `record_catalog_candidate` payload for this reading.

        `candidate_key` is deliberately absent: it is DERIVED from these
        structural fields by `backend.catalog.payloads`. `record_key` is passed
        so that derivation can bind the candidate to its own raw record.
        """
        return {"snapshot_id": record_row["snapshot_id"],
                "raw_record_id": record_row["id"],
                "record_key": record_row["record_key"],
                "manufacturer": self.manufacturer,
                "commercial_model": self.commercial_model,
                "model_year_start": self.model_year_start,
                "model_year_end": self.model_year_end,
                "official_model_code": self.official_model_code,
                "trim": self.trim,
                "identity_dimensions": dict(self.identity_dimensions),
                "status": self.status}


def _text(value: Any, *, limit: int) -> str | None:
    """The register's own text, trimmed of surrounding space, or absent.

    An empty or whitespace-only field is an ABSENCE -- the durable columns
    refuse `''` for exactly this reason: a blank reads as "stated" while saying
    nothing.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if len(stripped) > limit:
        raise GovernmentNormalizationError("GOV_NORM_IDENTITY_TOO_LONG")
    return stripped


def _whole(value: Any) -> int | None:
    return None if isinstance(value, bool) or not isinstance(value, int) else value


def _read_coded(record: Mapping[str, Any], code_field: str, name_field: str,
                table: Mapping[int, tuple[str, str]],
                uncoded: Mapping[str, str] | None = None) -> tuple[str | None, bool]:
    """One coded dimension, as `(value or None, settled)`.

    `settled` is False only in rule 4's case -- the register stated something
    this vocabulary cannot read. A declared-unknown marker and a genuinely
    absent field both return `(None, True)`: nothing was left unread.
    """
    code = _whole(record.get(code_field))
    label = record.get(name_field)
    if code is not None:
        paired = table.get(code)
        if paired is None:
            return None, False                      # rule 4: an unnamed code
        if _text(label, limit=200) != paired[1]:
            raise GovernmentNormalizationError("GOV_NORM_LABEL_CONTRADICTION")  # rule 2
        return paired[0], True
    if vocab.is_declared_unknown(label):
        return None, True                           # rule 3: the register says nothing
    stated = _text(label, limit=200)
    if stated is None:
        return None, True                           # the field is simply absent
    if uncoded is not None and stated in uncoded:
        # The register states this name with NO code at all, and a reviewer has
        # read the whole string. Exact, closed, and never reached while a code
        # exists.
        return uncoded[stated], True
    return None, False                              # rule 4: an unread label


def read_wltp_record(record: Mapping[str, Any], *,
                     resource_id: str = src.WLTP_RESOURCE_ID) -> RecordReading:
    """Read ONE row of the WLTP models resource, or refuse it.

    Deterministic in the strongest sense the rules allow: the result is a
    function of the row's VALUES only. Key order, insertion order and the page
    the row arrived on change nothing, because every field is addressed by name
    and every vocabulary lookup is a mapping lookup.
    """
    if resource_id != src.WLTP_RESOURCE_ID:
        # The quantity resource is allowlisted for CAPTURE -- its snapshots and
        # raw records are perfectly storable -- but it publishes different
        # columns and no reviewer has read them, so it has no identity
        # normalization here rather than a guessed one.
        raise GovernmentNormalizationError("GOV_NORM_RESOURCE_UNSUPPORTED")
    if not isinstance(record, Mapping):
        raise GovernmentNormalizationError("GOV_NORM_RECORD_SHAPE_INVALID")

    manufacturer = _text(record.get("tozar"), limit=MAX_MANUFACTURER_CHARS)
    if manufacturer is None:
        raise GovernmentNormalizationError("GOV_NORM_MANUFACTURER_MISSING")
    commercial_model = _text(record.get("kinuy_mishari"), limit=MAX_COMMERCIAL_MODEL_CHARS)
    if commercial_model is None:
        raise GovernmentNormalizationError("GOV_NORM_MODEL_MISSING")
    model_year = _whole(record.get("shnat_yitzur"))
    if model_year is None or not vocab.MIN_MODEL_YEAR <= model_year <= vocab.MAX_MODEL_YEAR:
        raise GovernmentNormalizationError("GOV_NORM_MODEL_YEAR_INVALID")

    dimensions: dict[str, str] = {}
    unresolved: list[str] = []
    for name, (code_field, name_field, table, uncoded) in _CODED_DIMENSIONS.items():
        value, settled = _read_coded(record, code_field, name_field, table, uncoded)
        if value is not None:
            dimensions[name] = value
        elif not settled:
            unresolved.append(name)

    body_style_label = _text(record.get("merkav"), limit=120)
    if body_style_label is not None and not vocab.is_declared_unknown(body_style_label):
        body_style = vocab.BODY_STYLE_BY_MERKAV.get(body_style_label)
        if body_style is None:
            unresolved.append("body_style")
        else:
            dimensions["body_style"] = body_style

    # Fuel and propulsion are two independent statements; both resolved and
    # incompatible is a contradiction in the ROW, not an unread dimension.
    fuel, propulsion = dimensions.get("fuel_type"), dimensions.get("propulsion_technology")
    if fuel is not None and propulsion is not None \
            and (fuel, propulsion) not in vocab.CONSISTENT_FUEL_PROPULSION:
        raise GovernmentNormalizationError("GOV_NORM_FUEL_PROPULSION_CONTRADICTION")

    displacement = _whole(record.get("nefah_manoa"))
    return RecordReading(
        upstream_record_id=str(record.get(vocab.GOVERNMENT_RECORD_ID_FIELD)),
        manufacturer=manufacturer, commercial_model=commercial_model,
        model_year_start=model_year, model_year_end=model_year,
        official_model_code=_text(record.get("degem_nm"), limit=MAX_MODEL_CODE_CHARS),
        trim=_text(record.get("ramat_gimur"), limit=MAX_TRIM_CHARS),
        identity_dimensions=dimensions, unresolved_dimensions=tuple(sorted(unresolved)),
        engine_displacement_cc=displacement if displacement and displacement > 0 else None)


#: dimension -> (code field, name field, closed code table, uncoded-name table).
#: Every key is in `CANDIDATE_IDENTITY_DIMENSIONS`, asserted below, so this
#: normalizer cannot name a dimension the durable schema would refuse.
_CODED_DIMENSIONS: Mapping[str, tuple[str, str, Mapping[int, tuple[str, str]], Mapping[str, str] | None]] = {
    "fuel_type": ("delek_cd", "delek_nm", vocab.FUEL_BY_CODE, None),
    "propulsion_technology": ("technologiat_hanaa_cd", "technologiat_hanaa_nm",
                              vocab.PROPULSION_BY_CODE, vocab.PROPULSION_BY_UNCODED_NAME),
    "drivetrain": ("hanaa_cd", "hanaa_nm", vocab.DRIVETRAIN_BY_CODE, None),
}

assert set(_CODED_DIMENSIONS) | {"body_style"} <= set(CANDIDATE_IDENTITY_DIMENSIONS)

#: The captured fields this normalizer deliberately does not read, with the
#: reviewer's reason for each. Reported beside a reading so a real gap travels
#: with the material instead of being invisible.
UNMAPPED_FIELDS: tuple[tuple[str, str], ...] = vocab.unmapped_fields(
    "koah_sus", "dg_metach_solela", "mishkal_kolel", "automatic_ind", "sug_degem")


__all__ = ["GOVERNMENT_NORMALIZATION_REASONS", "MAX_COMMERCIAL_MODEL_CHARS",
           "MAX_MANUFACTURER_CHARS", "MAX_MODEL_CODE_CHARS", "MAX_TRIM_CHARS",
           "UNMAPPED_FIELDS", "GovernmentNormalizationError", "RecordReading",
           "read_wltp_record"]
