"""PR-L1 (D2): the read-only discovery tree over the catalog variants.

manufacturers -> models -> years -> variants, plus the filter facets. Every
read is bounded and paged in the database (`catalog_browser_*`, migration
20260930000100): the site never pulls the register in bulk (rule 20).

Behind `MILO_ENABLE_CATALOG_BROWSER` (default off; a Stage A flag every deploy
pins off and `website-execution-activate.sh --apply-catalog-browser`
restores). Off, the surface does not exist: 404. GET only, behind the
existing authentication; a project must belong to the caller and read the
Government catalog.
"""

from __future__ import annotations

import os
import re
from typing import Any, Mapping
from uuid import UUID

from backend.catalog.government import vocabulary as vocab
from backend.catalog.register.service import _supported
from backend.catalog.register.variants import SEGMENTS
from backend.errors import AppError
from backend.production_config import TRUE_VALUES

BROWSER_FLAG = "MILO_ENABLE_CATALOG_BROWSER"
LEVELS = ("manufacturers", "models", "years", "variants", "facets")
DEFAULT_LIMIT = 50
MAX_LIMIT = 100
MAX_OFFSET = 100_000
MAX_VALUE_CHARS = 200

BROWSER_REASONS: Mapping[str, tuple[int, str]] = {
    "CATALOG_BROWSER_DISABLED": (404, "the catalog browser is not enabled"),
    "CATALOG_BROWSER_QUERY_INVALID": (422, "the catalog browser query is not valid"),
}


def _refusal(code: str) -> AppError:
    status, message = BROWSER_REASONS[code]
    return AppError(code, message, status)


def browser_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return (source.get(BROWSER_FLAG) or "").strip().lower() in TRUE_VALUES


def _authorize(repo: Any, user_id: UUID, project_id: UUID, env: Mapping[str, str] | None) -> None:
    if not browser_enabled(env) or not _supported(repo.get_project(project_id, user_id)):
        raise _refusal("CATALOG_BROWSER_DISABLED")


_WHOLE = re.compile(r"^-?[0-9]{1,9}$")


def _int(params: Mapping[str, Any], name: str, low: int, high: int, default: int | None = None) -> int | None:
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    text = str(raw).strip()
    if not _WHOLE.fullmatch(text) or not low <= int(text) <= high:
        raise _refusal("CATALOG_BROWSER_QUERY_INVALID")
    return int(text)


def _text(params: Mapping[str, Any], name: str, *, required: bool = False) -> str | None:
    raw = params.get(name)
    if raw is None or raw == "":
        if required:
            raise _refusal("CATALOG_BROWSER_QUERY_INVALID")
        return None
    text = str(raw)
    if len(text) > MAX_VALUE_CHARS or any(ord(char) < 32 for char in text):
        raise _refusal("CATALOG_BROWSER_QUERY_INVALID")
    return text


def _filters(params: Mapping[str, Any]) -> dict[str, Any]:
    segment = _text(params, "segment")
    if segment is not None and segment not in SEGMENTS:
        raise _refusal("CATALOG_BROWSER_QUERY_INVALID")
    year_from = _int(params, "year_from", vocab.MIN_MODEL_YEAR, vocab.MAX_MODEL_YEAR)
    year_to = _int(params, "year_to", vocab.MIN_MODEL_YEAR, vocab.MAX_MODEL_YEAR)
    if year_from is not None and year_to is not None and year_from > year_to:
        raise _refusal("CATALOG_BROWSER_QUERY_INVALID")
    return {"segment": segment, "year_from": year_from, "year_to": year_to,
            "delek_cd": _int(params, "delek_cd", 0, 99_999), "merkav": _text(params, "merkav")}


def _page(params: Mapping[str, Any]) -> dict[str, int]:
    return {"limit": _int(params, "limit", 1, MAX_LIMIT, DEFAULT_LIMIT) or DEFAULT_LIMIT,
            "offset": _int(params, "offset", 0, MAX_OFFSET, 0) or 0}


def browse(repo: Any, user_id: UUID, project_id: UUID, level: str, params: Mapping[str, Any], *,
           env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """One level of the tree, or the facets. `params` are the query string.
    Anything else -- and everything while the flag is off -- does not exist."""
    _authorize(repo, user_id, project_id, env)
    if level not in LEVELS:
        raise _refusal("CATALOG_BROWSER_DISABLED")
    if level == "facets":
        return repo.catalog_browser_facets(_text(params, "tozar"))
    filters, page = _filters(params), _page(params)
    if level == "manufacturers":
        answer = repo.catalog_browser_manufacturers(filters, **page)
        # PR-D3: the approved canonical manufacturer beside each exact tozar
        # (every filter and "Add to plan" keep the tozar).
        from backend.catalog.register.normalization import current_map

        canonical = current_map(repo)
        return {**answer, "items": [{**item, "canonical_manufacturer": canonical.get(str(item.get("tozar")))}
                                    for item in answer.get("items") or []]}
    tozar = _text(params, "tozar", required=True)
    if level == "models":
        return repo.catalog_browser_models(tozar, filters, **page)
    model = _text(params, "kinuy_mishari", required=True)
    if level == "years":
        return repo.catalog_browser_years(tozar, model, filters, **page)
    year = _int(params, "shnat_yitzur", vocab.MIN_MODEL_YEAR, vocab.MAX_MODEL_YEAR)
    if year is None:
        raise _refusal("CATALOG_BROWSER_QUERY_INVALID")
    return repo.catalog_browser_variants(tozar, model, year, filters, **page)


__all__ = ["BROWSER_FLAG", "BROWSER_REASONS", "LEVELS", "browse", "browser_enabled"]
