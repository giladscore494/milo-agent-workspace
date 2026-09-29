"""PR-L1: deterministic catalog variants -- level 1 (identity) and level 1.5
(every mapped Government field) as typed columns, the vehicle category (D4),
and the bounded build. No model, $0 (owner decisions 16, 22, 26, 27).

Every register row of an ACTIVE, count-verified, whole-tozar snapshot is its
own variant (decision 22). `map_record` turns ONE stored payload into ONE typed
row by a closed, code-owned field table; `build_snapshot_variants` writes a
snapshot's rows in bounded batches through `record_catalog_variants`
(migration 20260930000100), which also refreshes the coverage ledger at the
levels `identity` and `government_fields`.

The mapping rules (plan section 3.9)
------------------------------------

* Values are the register's own. Empty or missing -> null. No guessing, no
  defaults, no unit conversion: a numeric field is parsed from the register's
  own number or numeric string, and a value that does not parse is null with a
  recorded parse issue -- never a failure of the row or the snapshot.
* `sug_tkina_*` is the HOMOLOGATION STANDARD (`אמריקאית`, ...), never the
  transmission. Whether a vehicle is automatic comes from `automatic_ind` only.
* `sug_mamir` code 0 (`לא ידוע קוד 0`) is the register saying it has nothing
  to state: both columns null.
* The register publishes no fuel consumption, and nothing is computed from
  CO2: the CO2 fields are stored exactly as stated.
* Normalised values exist only where PR-V's vocabulary defines them
  (`normalize.read_wltp_record`: fuel, propulsion, drivetrain, body style),
  in their own `norm_*` columns; the source columns are never changed.
* The driver-assistance indicators and their sources go in ONE `equipment`
  document whose keys are the closed `EQUIPMENT_FIELDS`; any other key is
  refused (`check_equipment`, and the database's CHECK).

The category (D4)
-----------------

`vehicle_segment` comes from the register's `sug_degem` only, through
`SEGMENT_BY_SUG_DEGEM`; any other value -> ``unknown``. `merkav` and
`mishkal_kolel` were reviewed as inputs and deliberately are NOT used: the
WLTP register covers light vehicles only (every captured row is P or M, the
heaviest 3,540 kg), no `merkav` value names a truck, bus or motorcycle, and a
mass threshold would be a guess the register does not state. So the segments
it cannot support (truck, bus, motorcycle, other) are dropped.

`python -m backend.catalog.register.variants --snapshot-key KEY` is the
operator backfill of ONE active snapshot (scripts/ops/register-variants.sh).
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from backend.catalog.government import source as src
from backend.catalog.government import vocabulary as vocab
from backend.catalog.government.normalize import GovernmentNormalizationError, read_wltp_record

#: The mapper's version. A change to any table or rule below is a new
#: version (and `public.catalog_variant_mapper_version()` with it): its rows
#: are written beside the old ones, never over them.
MAPPER_VERSION = "gov.wltp.variant-mapper.1"

#: The most rows one build batch reads and writes (the database's bound too).
BUILD_BATCH_ROWS = 500

TEXT, INT, NUM, IND = "text", "int", "num", "ind"
MAX_TEXT_CHARS = 200
MAX_SOURCE_CHARS = 80
_INT32 = 2**31 - 1

#: Level 1 -- identity: (column, register field, kind).
LEVEL_1_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("tozar", "tozar", TEXT), ("tozeret_cd", "tozeret_cd", INT), ("tozeret_nm", "tozeret_nm", TEXT),
    ("tozeret_eretz_nm", "tozeret_eretz_nm", TEXT), ("degem_cd", "degem_cd", INT),
    ("degem_nm", "degem_nm", TEXT), ("sug_degem", "sug_degem", TEXT),
    ("kinuy_mishari", "kinuy_mishari", TEXT), ("shnat_yitzur", "shnat_yitzur", INT),
    ("ramat_gimur", "ramat_gimur", TEXT), ("delek_cd", "delek_cd", INT), ("delek_nm", "delek_nm", TEXT),
)

#: Level 1.5 -- every other mapped Government field. Columns are the register's
#: names, lower-cased (PostgreSQL folds `CO2_WLTP` to `co2_wltp`).
LEVEL_1_5_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("nefah_manoa", "nefah_manoa", INT), ("koah_sus", "koah_sus", INT),
    ("hanaa_cd", "hanaa_cd", INT), ("hanaa_nm", "hanaa_nm", TEXT),
    ("technologiat_hanaa_cd", "technologiat_hanaa_cd", INT),
    ("technologiat_hanaa_nm", "technologiat_hanaa_nm", TEXT),
    ("automatic_ind", "automatic_ind", IND),
    ("sug_tkina_cd", "sug_tkina_cd", INT), ("sug_tkina_nm", "sug_tkina_nm", TEXT),
    ("sug_mamir_cd", "sug_mamir_cd", INT), ("sug_mamir_nm", "sug_mamir_nm", TEXT),
    ("merkav", "merkav", TEXT), ("mispar_dlatot", "mispar_dlatot", INT),
    ("mispar_moshavim", "mispar_moshavim", INT), ("mishkal_kolel", "mishkal_kolel", INT),
    ("kosher_grira_im_blamim", "kosher_grira_im_blamim", INT),
    ("kosher_grira_bli_blamim", "kosher_grira_bli_blamim", INT),
    ("kamut_co2_city", "kamut_CO2_city", NUM), ("kamut_co2_hway", "kamut_CO2_hway", NUM),
    ("co2_wltp", "CO2_WLTP", NUM), ("nox_wltp", "NOX_WLTP", NUM), ("co_wltp", "CO_WLTP", NUM),
    ("hc_wltp", "HC_WLTP", NUM), ("kvutzat_zihum", "kvutzat_zihum", INT),
    ("madad_yarok", "madad_yarok", NUM), ("nikud_betihut", "nikud_betihut", NUM),
    ("ramat_eivzur_betihuty", "ramat_eivzur_betihuty", INT),
    ("mispar_kariot_avir", "mispar_kariot_avir", INT), ("abs_ind", "abs_ind", IND),
    # ESC, as the register names it.
    ("bakarat_yatzivut_ind", "bakarat_yatzivut_ind", IND),
)

#: The driver-assistance indicators (0/1) and the sources the register states
#: for five of them. CLOSED: `public.catalog_variant_equipment_valid` holds
#: the same list.
EQUIPMENT_INDICATORS: tuple[str, ...] = (
    "bakarat_mehirut_isa", "bakarat_shyut_adaptivit_ind", "bakarat_stiya_activ_s",
    "bakarat_stiya_menativ_ind", "blima_otomatit_nesia_leahor",
    "blimat_hirum_lifnei_holhei_regel_ofanaim", "hayshaney_hagorot_ind",
    "hayshaney_lahatz_avir_batzmigim_ind", "hitnagshut_cad_shetah_met", "maarechet_ezer_labalam_ind",
    "matzlemat_reverse_ind", "nitur_merhak_milfanim_ind", "shlita_automatit_beorot_gvohim_ind",
    "teura_automatit_benesiya_kadima_ind", "zihuy_beshetah_nistar_ind", "zihuy_holchey_regel_ind",
    "zihuy_matzav_hitkarvut_mesukenet_ind", "zihuy_rechev_do_galgali", "zihuy_tamrurey_tnua_ind",
)
EQUIPMENT_SOURCES: tuple[str, ...] = (
    "bakarat_stiya_menativ_makor_hatkana", "nitur_merhak_milfanim_makor_hatkana",
    "shlita_automatit_beorot_gvohim_makor_hatkana", "zihuy_holchey_regel_makor_hatkana",
    "zihuy_tamrurey_tnua_makor_hatkana",
)
EQUIPMENT_FIELDS: tuple[str, ...] = EQUIPMENT_INDICATORS + EQUIPMENT_SOURCES

#: `norm_*` column -> the PR-V normalization's dimension.
NORMALISED_COLUMNS: Mapping[str, str] = {
    "norm_fuel_type": "fuel_type", "norm_propulsion_technology": "propulsion_technology",
    "norm_drivetrain": "drivetrain", "norm_body_style": "body_style",
}

#: D4, owner-reviewed: the register's `sug_degem` -> vehicle segment.
#: P = פרטי (private vehicle model), M = מסחרי (commercial vehicle model), as
#: the Ministry's model register uses them; every other value -> `unknown`.
SEGMENT_BY_SUG_DEGEM: Mapping[str, str] = {"P": "private", "M": "commercial"}
UNKNOWN_SEGMENT = "unknown"
SEGMENTS: tuple[str, ...] = ("private", "commercial", UNKNOWN_SEGMENT)

#: The closed per-field parse issues (mirrored by the database's CHECK).
PARSE_REASONS: tuple[str, ...] = ("not_text", "too_long", "not_a_number", "not_a_whole_number",
                                  "out_of_range", "not_an_indicator")

#: This module's static refusals and failures.
VARIANT_REASONS: Mapping[str, str] = {
    "CATALOG_VARIANT_EQUIPMENT_KEY_UNKNOWN": "an equipment key is not in the closed list",
    "CATALOG_VARIANT_RECORD_INVALID": "a register row is not an object with an id",
    "CATALOG_VARIANT_SNAPSHOT_UNKNOWN": "no active Government snapshot has that key",
    "CATALOG_VARIANT_SNAPSHOT_INELIGIBLE":
        "only an active, count-verified, whole-tozar Government snapshot is built",
    "CATALOG_VARIANT_MAPPER_MISMATCH": "the database's mapper version is not this code's",
    "CATALOG_VARIANT_ROWS_INVALID": "the database refused a variant batch",
    "CATALOG_VARIANT_BUILD_FAILED": "the variant build did not complete",
}
#: The database's refusal codes the repository maps (a subset of the above).
DATABASE_REFUSALS: tuple[str, ...] = ("CATALOG_VARIANT_SNAPSHOT_INELIGIBLE",
                                      "CATALOG_VARIANT_MAPPER_MISMATCH", "CATALOG_VARIANT_ROWS_INVALID")


class VariantMappingError(ValueError):
    def __init__(self, reason_code: str) -> None:
        if reason_code not in VARIANT_REASONS:
            raise ValueError("variant reason must come from the static allowlist")
        super().__init__(reason_code)
        self.reason_code = reason_code


# -- parsing: the register's own values, or null plus an issue -----------------------

def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _parse(value: Any, kind: str, *, limit: int = MAX_TEXT_CHARS) -> tuple[Any, str | None]:
    """(value, issue): the parsed value or None, and a parse reason or None."""
    if _blank(value):
        return None, None
    if kind == TEXT:
        if not isinstance(value, str):
            return None, "not_text"
        return (value, None) if len(value) <= limit else (None, "too_long")
    not_a_number = "not_an_indicator" if kind == IND else "not_a_number"
    if isinstance(value, bool):
        return None, not_a_number
    if isinstance(value, str):
        try:
            number: Any = Decimal(value.strip())
        except InvalidOperation:
            return None, not_a_number
        if not number.is_finite():
            return None, not_a_number
    elif isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None, not_a_number
        number = value
    else:
        return None, not_a_number
    if kind == NUM:
        return (float(number) if not isinstance(number, int) else number), None
    if number != int(number):
        return None, "not_a_whole_number" if kind == INT else "not_an_indicator"
    whole = int(number)
    if kind == IND:
        return (whole, None) if whole in (0, 1) else (None, "not_an_indicator")
    return (whole, None) if -_INT32 - 1 <= whole <= _INT32 else (None, "out_of_range")


def vehicle_segment(sug_degem: Any) -> str:
    """D4: the code-owned segment of a register `sug_degem`; never a guess."""
    return SEGMENT_BY_SUG_DEGEM.get(sug_degem, UNKNOWN_SEGMENT) if isinstance(sug_degem, str) \
        else UNKNOWN_SEGMENT


def check_equipment(document: Mapping[str, Any]) -> Mapping[str, Any]:
    """Refuse any key outside the closed list, and any value of the wrong shape."""
    for key, value in document.items():
        if key not in EQUIPMENT_FIELDS:
            raise VariantMappingError("CATALOG_VARIANT_EQUIPMENT_KEY_UNKNOWN")
        if key in EQUIPMENT_SOURCES:
            if not isinstance(value, str) or not 1 <= len(value) <= MAX_SOURCE_CHARS:
                raise VariantMappingError("CATALOG_VARIANT_EQUIPMENT_KEY_UNKNOWN")
        elif isinstance(value, bool) or value not in (0, 1):
            raise VariantMappingError("CATALOG_VARIANT_EQUIPMENT_KEY_UNKNOWN")
    return document


def _normalised(payload: Mapping[str, Any]) -> dict[str, str | None]:
    """PR-V's reading of the row, where its vocabulary defines one."""
    try:
        dimensions = read_wltp_record(payload).identity_dimensions
    except GovernmentNormalizationError:
        dimensions = {}
    return {column: dimensions.get(name) for column, name in NORMALISED_COLUMNS.items()}


def map_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    """ONE stored register payload -> ONE typed variant row (pure)."""
    if not isinstance(payload, Mapping) or _blank(payload.get(vocab.GOVERNMENT_RECORD_ID_FIELD)):
        raise VariantMappingError("CATALOG_VARIANT_RECORD_INVALID")
    row: dict[str, Any] = {"upstream_record_id": str(payload[vocab.GOVERNMENT_RECORD_ID_FIELD])}
    issues: list[dict[str, str]] = []
    for column, name, kind in LEVEL_1_FIELDS + LEVEL_1_5_FIELDS:
        value, issue = _parse(payload.get(name), kind)
        row[column] = value
        if issue:
            issues.append({"field": name, "reason": issue})
    # `לא ידוע קוד 0`: the register states no converter type.
    if row["sug_mamir_cd"] == 0 or vocab.is_declared_unknown(row["sug_mamir_nm"]):
        row["sug_mamir_cd"] = row["sug_mamir_nm"] = None
    equipment: dict[str, Any] = {}
    for name in EQUIPMENT_FIELDS:
        value, issue = _parse(payload.get(name), TEXT if name in EQUIPMENT_SOURCES else IND,
                              limit=MAX_SOURCE_CHARS)
        if value is not None:
            equipment[name] = value
        if issue:
            issues.append({"field": name, "reason": issue})
    row["equipment"] = dict(check_equipment(equipment))
    row["parse_issues"] = issues
    row.update(_normalised(payload))
    row["vehicle_segment"] = vehicle_segment(row["sug_degem"])
    return row


# -- the build ------------------------------------------------------------------------

def build_snapshot_variants(repository: Any, snapshot_id: Any, *,
                            batch_rows: int = BUILD_BATCH_ROWS) -> dict[str, Any]:
    """Build ONE snapshot's variants in bounded batches; idempotent.

    A build already complete under this mapper version is answered as it is
    (`unchanged`) without reading a row. Otherwise every page of at most
    `batch_rows` raw records is mapped and written, one page in memory at a
    time; rows already built are left exactly as they are by the database.
    Raises what the repository raises (a refusal carries a static code).
    """
    bound = max(1, min(int(batch_rows), BUILD_BATCH_ROWS))
    state = repository.catalog_variant_build_state(str(snapshot_id), MAPPER_VERSION)
    if state and state.get("completed_at"):
        return {"status": "unchanged", "snapshot_key": state.get("snapshot_key"),
                "built_rows": state.get("built_rows"), "expected_rows": state.get("expected_rows"),
                "inserted": 0, "batches": 0, "rows_with_parse_issues": 0}
    report: dict[str, Any] = {"status": "incomplete", "snapshot_key": None, "built_rows": 0,
                              "expected_rows": None, "inserted": 0, "batches": 0,
                              "rows_with_parse_issues": 0}
    offset = 0
    while True:
        page = repository.list_catalog_raw_records(snapshot_id, limit=bound, offset=offset)
        if not page:
            break
        rows = [map_record(record["payload"]) for record in page]
        answer = repository.record_catalog_variants(str(snapshot_id), MAPPER_VERSION, rows)
        report["batches"] += 1
        report["inserted"] += int(answer.get("inserted") or 0)
        report["rows_with_parse_issues"] += sum(1 for row in rows if row["parse_issues"])
        report.update(snapshot_key=answer.get("snapshot_key"), built_rows=answer.get("built_rows"),
                      expected_rows=answer.get("expected_rows"),
                      status="built" if answer.get("complete") else "incomplete")
        offset += len(page)
        if len(page) < bound:
            break
    return report


def _static_code(failure: BaseException) -> str:
    code = getattr(failure, "code", None) or getattr(failure, "reason_code", None)
    return code if isinstance(code, str) and code in VARIANT_REASONS else "CATALOG_VARIANT_BUILD_FAILED"


def build_after_capture(repository: Any, snapshot_id: Any) -> dict[str, Any]:
    """The capture job's build of a snapshot it just marked captured. Never
    raises: a failed build leaves the unit captured and is reported with a
    static code (the operator backfill builds it again)."""
    try:
        report = build_snapshot_variants(repository, snapshot_id)
    except Exception as failure:  # noqa: BLE001 - reduced to a static code
        return {"status": "failed", "code": _static_code(failure)}
    return {"status": report["status"], "built_rows": report["built_rows"],
            "expected_rows": report["expected_rows"]}


# -- the operator backfill: one snapshot_key at a time --------------------------------

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2
_SNAPSHOT_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")


def _open_repository() -> Any:  # pragma: no cover - production wiring
    from backend.config import get_settings
    from backend.repository import SupabaseRepository

    return SupabaseRepository(get_settings())


def main(argv: Sequence[str] | None = None, *, repository: Any = None) -> int:
    """Prints one outcome line: BUILT / REFUSED / FAILED, a static code, the
    snapshot key and counts -- no register value, no URL, no secret."""
    parser = argparse.ArgumentParser(prog="python -m backend.catalog.register.variants")
    parser.add_argument("--snapshot-key", required=True)
    args, extra = parser.parse_known_args(list(argv or []))
    if extra or not _SNAPSHOT_KEY.fullmatch(args.snapshot_key):
        print("REFUSED CATALOG_VARIANT_REQUEST_INVALID: --snapshot-key must be one snapshot key")
        return EXIT_REFUSED
    repo = repository if repository is not None else _open_repository()
    try:
        snapshot = repo.find_active_catalog_snapshot("government", src.WLTP_RESOURCE_ID, args.snapshot_key)
    except Exception:
        print("FAILED CATALOG_VARIANT_BUILD_FAILED: the snapshot could not be read")
        return EXIT_FAILED
    if snapshot is None:
        print(f"REFUSED CATALOG_VARIANT_SNAPSHOT_UNKNOWN: {args.snapshot_key}")
        return EXIT_REFUSED
    try:
        report = build_snapshot_variants(repo, snapshot["id"])
    except Exception as failure:  # noqa: BLE001 - reduced to a static code
        code = _static_code(failure)
        refused = code in ("CATALOG_VARIANT_SNAPSHOT_INELIGIBLE", "CATALOG_VARIANT_MAPPER_MISMATCH")
        print(f"{'REFUSED' if refused else 'FAILED'} {code}: {args.snapshot_key}")
        return EXIT_REFUSED if refused else EXIT_FAILED
    line = (f"snapshot_key={args.snapshot_key} status={report['status']} "
            f"rows={report['built_rows']}/{report['expected_rows']} inserted={report['inserted']} "
            f"batches={report['batches']} rows_with_parse_issues={report['rows_with_parse_issues']} "
            f"mapper_version={MAPPER_VERSION}")
    if report["status"] in ("built", "unchanged"):
        print(f"BUILT {line}")
        return EXIT_OK
    print(f"FAILED CATALOG_VARIANT_BUILD_FAILED: {line}")
    return EXIT_FAILED


__all__ = ["BUILD_BATCH_ROWS", "DATABASE_REFUSALS", "EQUIPMENT_FIELDS", "EQUIPMENT_INDICATORS",
           "EQUIPMENT_SOURCES", "LEVEL_1_5_FIELDS", "LEVEL_1_FIELDS", "MAPPER_VERSION",
           "NORMALISED_COLUMNS", "PARSE_REASONS", "SEGMENTS", "SEGMENT_BY_SUG_DEGEM", "VARIANT_REASONS",
           "VariantMappingError", "build_after_capture", "build_snapshot_variants", "check_equipment",
           "main", "map_record", "vehicle_segment"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
