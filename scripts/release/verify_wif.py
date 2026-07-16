"""Exact verification of the Vercel -> GCP Workload Identity Federation chain.

Given the provider describe JSON, the gateway service-account IAM policy JSON
and the API service run-invoker IAM policy JSON, plus the EXACT expected
values, emit `STATUS|name|detail` findings. Nothing here passes merely because
a role has at least one member — every set is compared exactly and broad
principals (allUsers/allAuthenticatedUsers) and unrelated members are rejected.

This is a distinct trust chain from GitHub Actions -> GCP (which only
authenticates the bootstrap workflow); the two must never share ambiguous
generic names.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

_FINDINGS: list[str] = []


def emit(status: str, name: str, detail: str) -> None:
    detail = detail.replace("\n", " ").replace("|", "/")
    _FINDINGS.append(f"{status}|{name}|{detail}")


def _load(raw: str):
    try:
        return json.loads(raw)
    except Exception:
        return None


def _norm_condition(c: str) -> str:
    # Whitespace-insensitive comparison of a CEL attribute condition.
    return re.sub(r"\s+", " ", (c or "").strip())


def _members(policy: dict, role: str) -> list:
    out: list[str] = []
    for b in (policy.get("bindings") or []):
        if isinstance(b, dict) and b.get("role") == role:
            for m in (b.get("members") or []):
                if isinstance(m, str) and m not in out:
                    out.append(m)
    return out


BROAD = {"allUsers", "allAuthenticatedUsers"}


def check_provider(pjson: str, issuer: str, audience: str, condition: str, mapping_json: str) -> None:
    prov = _load(pjson)
    if not isinstance(prov, dict):
        emit("BLOCKED", "wif:provider", "could not parse WIF provider description (fail closed)")
        return
    oidc = prov.get("oidc") or {}
    got_issuer = str(oidc.get("issuerUri") or "")
    if got_issuer != issuer:
        emit("BLOCKED", "wif:issuer", f"provider issuerUri '{got_issuer}' does not equal expected '{issuer}'")
    else:
        emit("PASS", "wif:issuer", f"issuer equals '{issuer}'")

    auds = oidc.get("allowedAudiences") or []
    auds = [str(a) for a in auds] if isinstance(auds, list) else []
    if set(auds) != {audience}:
        emit("BLOCKED", "wif:audience", f"allowedAudiences {sorted(auds)} is not exactly {{'{audience}'}}")
    else:
        emit("PASS", "wif:audience", "allowed audience set matches exactly")

    # EXACT attribute-mapping comparison: the full dictionary must equal the
    # expected mapping (same keys, same expressions, no missing, no extras).
    got_mapping = prov.get("attributeMapping")
    got_mapping = got_mapping if isinstance(got_mapping, dict) else {}
    try:
        want_mapping = json.loads(mapping_json) if mapping_json else None
    except Exception:
        want_mapping = None
    if not isinstance(want_mapping, dict) or not want_mapping:
        emit("BLOCKED", "wif:attribute-mapping", "no expected attributeMapping supplied; cannot prove the mapping (fail closed)")
    else:
        got_norm = {str(k): str(v) for k, v in got_mapping.items()}
        want_norm = {str(k): str(v) for k, v in want_mapping.items()}
        missing = sorted(set(want_norm) - set(got_norm))
        extra = sorted(set(got_norm) - set(want_norm))
        wrong = sorted(k for k in want_norm if k in got_norm and got_norm[k] != want_norm[k])
        if missing or extra or wrong:
            emit("BLOCKED", "wif:attribute-mapping", f"attributeMapping mismatch — missing {missing}, extra {extra}, wrong-expression {wrong}")
        else:
            emit("PASS", "wif:attribute-mapping", "attributeMapping matches the expected mapping exactly (keys + expressions, no extras)")

    got_cond = _norm_condition(str(prov.get("attributeCondition") or ""))
    if not got_cond:
        emit("BLOCKED", "wif:attribute-condition", "provider has NO attribute condition; an unconditioned provider is rejected")
    elif got_cond != _norm_condition(condition):
        emit("BLOCKED", "wif:attribute-condition", "attribute condition does not match the expected policy")
    else:
        emit("PASS", "wif:attribute-condition", "attribute condition matches exactly")


def check_gateway_binding(gjson: str, principal_set: str) -> None:
    pol = _load(gjson)
    if not isinstance(pol, dict):
        emit("BLOCKED", "wif:gateway-binding", "could not parse gateway SA IAM policy (fail closed)")
        return
    members = _members(pol, "roles/iam.workloadIdentityUser")
    broad = [m for m in members if m in BROAD]
    if broad:
        emit("BLOCKED", "wif:gateway-binding", f"workloadIdentityUser has broad principal(s) {broad}")
        return
    if set(members) != {principal_set}:
        emit("BLOCKED", "wif:gateway-binding", f"workloadIdentityUser members {sorted(members)} are not exactly {{expected principalSet}}")
        return
    emit("PASS", "wif:gateway-binding", "gateway SA workloadIdentityUser is bound to exactly the expected principalSet")


def check_run_invoker(rjson: str, gateway_sa: str) -> None:
    pol = _load(rjson)
    if not isinstance(pol, dict):
        emit("BLOCKED", "wif:run-invoker", "could not parse API service IAM policy (fail closed)")
        return
    members = _members(pol, "roles/run.invoker")
    broad = [m for m in members if m in BROAD]
    if broad:
        emit("BLOCKED", "wif:run-invoker", f"run.invoker has broad principal(s) {broad}; the API must stay private")
        return
    expected = f"serviceAccount:{gateway_sa}"
    if set(members) != {expected}:
        emit("BLOCKED", "wif:run-invoker", f"run.invoker members {sorted(members)} are not exactly {{{expected}}}")
        return
    emit("PASS", "wif:run-invoker", "run.invoker on the API service is exactly the gateway SA (no broad or unrelated members)")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--provider-json", default="")
    p.add_argument("--gateway-policy-json", default="")
    p.add_argument("--run-policy-json", default="")
    p.add_argument("--expected-issuer", default="")
    p.add_argument("--expected-audience", default="")
    p.add_argument("--expected-attribute-condition", default="")
    p.add_argument("--expected-attribute-mapping", default="")
    p.add_argument("--expected-principal-set", default="")
    p.add_argument("--gateway-sa", required=True)
    args = p.parse_args()
    if args.provider_json:
        check_provider(args.provider_json, args.expected_issuer, args.expected_audience,
                       args.expected_attribute_condition, args.expected_attribute_mapping)
    if args.gateway_policy_json:
        check_gateway_binding(args.gateway_policy_json, args.expected_principal_set)
    if args.run_policy_json:
        check_run_invoker(args.run_policy_json, args.gateway_sa)
    for line in _FINDINGS:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
