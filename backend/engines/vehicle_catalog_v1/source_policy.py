# -*- coding: utf-8 -*-
"""Deterministic Israel-market source policy for vehicle_catalog_v1.

Classifies evidence URLs into the existing verifier source-strength
vocabulary (``official_israel | israeli_auto_portal | used_market |
global_official | foreign_market | weak``) extended with the stronger
``government`` and ``importer_israel`` classes (in Israel the authorized
importer IS the official channel, so manufacturer-branded ``.il`` domains
classify as ``official_israel``; ``importer_israel`` is kept in the ranking
for callers that distinguish the two).

Evidence policy enforced on the deterministic final output:

- global/foreign sources may keep supporting technical fields, but
- foreign-only evidence never establishes normal official Israeli-market
  presence and never keeps ``high`` confidence as Israeli-market evidence;
- candidates supported only by foreign/global/weak evidence are downgraded
  to ``needs_review`` (preserved, never hidden);
- credible Israeli evidence (government / official importer / Israeli
  portal / Israeli used market) passes untouched.

The rules are generic (parameterized by manufacturer name) — no
manufacturer-specific business rules are hard-coded.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

SOURCE_CLASS_RANK: Dict[str, int] = {
    "government": 7,
    "official_israel": 6,
    "importer_israel": 5,
    "israeli_auto_portal": 4,
    "used_market": 3,
    "global_official": 2,
    "foreign_market": 1,
    "weak": 0,
    "unknown": 0,
}

# Classes that count as Israeli-market evidence.
ISRAEL_MARKET_CLASSES = {"government", "official_israel", "importer_israel", "israeli_auto_portal", "used_market"}

# Well-known Israeli used-vehicle marketplaces (generic, not per-manufacturer).
ISRAELI_USED_MARKET_HOSTS = ("yad2.co.il",)

FOREIGN_ONLY_NOTE = (
    "Source policy: no Israeli-market evidence — foreign/global sources "
    "cannot establish official Israeli availability"
)


def _host(url: Any) -> str:
    if not url or not isinstance(url, str):
        return ""
    text = url.strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    try:
        host = urlparse(text).hostname or ""
    except ValueError:
        return ""
    return host.lower().strip(".")


def _manufacturer_token(manufacturer: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(manufacturer or "").lower())


def classify_source(url: Any, manufacturer: Any = "") -> str:
    """Deterministically classify one evidence URL."""
    host = _host(url)
    if not host:
        return "weak"
    token = _manufacturer_token(manufacturer)
    compact_host = host.replace("-", "")

    if host == "gov.il" or host.endswith(".gov.il"):
        return "government"
    if host.endswith(".il"):
        if any(host == known or host.endswith("." + known) for known in ISRAELI_USED_MARKET_HOSTS):
            return "used_market"
        if token and token in compact_host:
            # Manufacturer-branded Israeli domain: the authorized
            # importer/official channel (e.g. <manufacturer>.co.il).
            return "official_israel"
        return "israeli_auto_portal"

    labels = host.split(".")
    tld = labels[-1] if labels else ""
    registered = ".".join(labels[-2:]) if len(labels) >= 2 else host
    if token:
        if registered in {f"{token}.com", f"{token}.net", f"{token}.org"} and registered.split(".", 1)[0] == token:
            # The bare global manufacturer domain (incl. www./global. hosts).
            return "global_official"
        if token in compact_host:
            # Manufacturer-branded but market-specific or dealer domain
            # (e.g. <manufacturer>usa.com, <manufacturer>of<city>.com,
            # <manufacturer>.com.vn) — a foreign market, never Israel.
            return "foreign_market"
    if len(tld) == 2 and tld != "il":
        return "foreign_market"
    return "weak"


def evaluate_sources(sources: Any, manufacturer: Any = "") -> Dict[str, Any]:
    """Classify a model's evidence set and decide Israeli-market presence."""
    urls = [u for u in (sources or []) if u] if isinstance(sources, list) else []
    classes = [classify_source(u, manufacturer) for u in urls]
    best = "weak"
    for cls in classes:
        if SOURCE_CLASS_RANK.get(cls, 0) > SOURCE_CLASS_RANK.get(best, 0):
            best = cls
    return {
        "source_classes": classes,
        "best_source_class": best,
        "israel_market_evidence": any(cls in ISRAEL_MARKET_CLASSES for cls in classes),
    }


def apply_israel_source_policy(final_json: Any, manufacturer: Any = "") -> Any:
    """Enforce the Israel evidence policy on the deterministic final output.

    Downgrades foreign/global/weak-only candidates to ``needs_review`` with
    confidence capped at ``medium`` (mirroring the verifier's existing
    downgrade behavior), annotates every model with its deterministic
    ``source_policy`` verdict, and recomputes the ``needs_review`` /
    ``rejected`` views so they stay consistent. Candidates are preserved,
    never removed."""
    if not isinstance(final_json, dict) or not isinstance(final_json.get("models"), list):
        return final_json
    for model in final_json["models"]:
        if not isinstance(model, dict):
            continue
        verdict = evaluate_sources(model.get("sources"), manufacturer)
        model["source_policy"] = verdict
        if verdict["israel_market_evidence"]:
            continue
        if model.get("verification_status") != "rejected":
            model["verification_status"] = "needs_review"
        if str(model.get("confidence") or "").lower() == "high":
            model["confidence"] = "medium"
        existing_notes = model.get("verification_notes")
        if FOREIGN_ONLY_NOTE not in str(existing_notes or ""):
            model["verification_notes"] = (f"{existing_notes}; " if existing_notes else "") + FOREIGN_ONLY_NOTE
    final_json["needs_review"] = [m for m in final_json["models"] if isinstance(m, dict) and m.get("verification_status") in {"partial", "needs_review"}]
    final_json["rejected"] = [m for m in final_json["models"] if isinstance(m, dict) and m.get("verification_status") == "rejected"]
    return final_json


def market_requires_israel_policy(market: Optional[str]) -> bool:
    return "israel" in (market or "").lower()
