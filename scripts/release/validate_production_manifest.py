"""Schema validation for the non-secret production release manifest.

Parses the constrained YAML subset used by config/production.example.yaml
(nested string-keyed maps, string/boolean scalars, block and inline string
lists) without external dependencies, then validates:

- every required key exists and is non-empty;
- placeholder values are rejected in --mode apply (allowed in plan mode,
  where the template itself must validate);
- wildcard CORS origins are always rejected;
- API and worker identities must differ; gateway and worker identities
  must differ;
- release and rollback SHAs must be full 40-hex immutable identifiers in
  apply mode (mutable tags such as latest/prod/stable are always rejected);
- a rollback SHA must be present;
- every execution flag must be false (Stage A posture);
- secret entries carry resource NAMES only — any value that looks like
  secret material is rejected.

Exit status: 0 valid, 1 invalid, 64 usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PLACEHOLDER_RE = re.compile(r"^<.*>$")
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MUTABLE_TAGS = {"latest", "prod", "stable", "main", "master", "production"}
SECRET_MATERIAL_RE = re.compile(r"(sk-[A-Za-z0-9_-]{10,}|BEGIN [A-Z ]*PRIVATE KEY|eyJ[A-Za-z0-9_-]{20,})")

REQUIRED_SCALARS = [
    ("gcp", "project_id"),
    ("gcp", "region"),
    ("gcp", "artifact_registry_repository"),
    ("gcp", "cloud_run_api_service"),
    ("gcp", "cloud_run_worker_job"),
    ("identities", "api_service_account"),
    ("identities", "worker_service_account"),
    ("identities", "gateway_identity"),
    ("identities", "deploy_operator"),
    ("supabase", "project_ref"),
    ("vercel", "project_name"),
    ("redis", "logical_environment"),
    ("release", "sha"),
    ("release", "rollback_sha"),
]

EXECUTION_FLAGS = [
    "MILO_ENABLE_RUN_CREATION",
    "MILO_ENABLE_PROPOSAL_MUTATIONS",
    "MILO_ENABLE_PROPOSAL_READS",
    "MILO_ENABLE_RUN_CANCELLATION",
    "MILO_ENABLE_EXECUTION_CONTROL",
    "MILO_ENABLE_PAID_EXECUTION",
    "GATEWAY_ALLOW_EXECUTION_ROUTES",
]


def parse_manifest(text: str) -> dict:
    """Parse the constrained YAML subset used by the manifest."""
    root: dict = {}
    # Stack of (indent, container) where container is a dict or list.
    stack: list[tuple[int, object]] = [(-1, root)]
    pending_key: tuple[int, dict, str] | None = None

    def scalar(raw: str):
        raw = raw.strip()
        if raw.startswith("[") or raw.startswith("{"):
            return json.loads(raw)
        if raw.lower() == "true":
            return True
        if raw.lower() == "false":
            return False
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            return raw[1:-1]
        return raw

    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.split("#", 1)[0].rstrip() if not line.lstrip().startswith("#") else ""
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip())
        content = stripped.strip()

        if pending_key is not None:
            pk_indent, pk_dict, pk_name = pending_key
            if indent > pk_indent:
                container: object = [] if content.startswith("- ") else {}
                pk_dict[pk_name] = container
                stack.append((pk_indent, container))
                pending_key = None
            else:
                pk_dict[pk_name] = None
                pending_key = None

        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise ValueError(f"line {lineno}: bad indentation")
        container = stack[-1][1]

        if content.startswith("- "):
            if not isinstance(container, list):
                raise ValueError(f"line {lineno}: list item outside a list")
            container.append(scalar(content[2:]))
            stack.append((indent - 1, container))  # keep list active at this indent
            continue
        if ":" not in content:
            raise ValueError(f"line {lineno}: expected 'key: value'")
        key, _, value = content.partition(":")
        key = key.strip()
        if not isinstance(container, dict):
            raise ValueError(f"line {lineno}: mapping entry inside a list")
        if value.strip() == "":
            pending_key = (indent, container, key)
        else:
            container[key] = scalar(value)
    if pending_key is not None:
        pending_key[1][pending_key[2]] = None
    return root


def get(data: dict, *path):
    cur = data
    for part in path:
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


# Logical secret consumer -> the manifest identity that must hold access.
CONSUMER_IDENTITY = {
    "api": ("identities", "api_service_account"),
    "worker": ("identities", "worker_service_account"),
    "gateway": ("identities", "gateway_identity"),
}


def _is_placeholder_value(value) -> bool:
    if not isinstance(value, str) or not value.strip():
        return True
    v = value.strip()
    return bool(PLACEHOLDER_RE.match(v)) or "changeme" in v.lower() or "placeholder" in v.lower()


def emit_secret_consumers(data: dict) -> list[str]:
    """Map each fully-specified secret to `name=email[,email]` expectations.

    Only secrets whose resource name AND every consumer's mapped
    service-account email are concrete (non-placeholder) are emitted, so the
    placeholder template yields nothing and can never be mistaken for a real
    Secret Manager verification.
    """
    lines: list[str] = []
    secrets = get(data, "secrets")
    if not isinstance(secrets, dict):
        return lines
    for _label, entry in secrets.items():
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        consumers = entry.get("consumers")
        if _is_placeholder_value(name):
            continue
        if not isinstance(consumers, list) or not consumers:
            continue
        emails: list[str] = []
        ok = True
        for consumer in consumers:
            path = CONSUMER_IDENTITY.get(consumer)
            if path is None:
                ok = False
                break
            email = get(data, *path)
            if _is_placeholder_value(email):
                ok = False
                break
            emails.append(email)
        if ok and emails:
            lines.append(f"{name}={','.join(emails)}")
    return lines


def validate(data: dict, mode: str) -> list[str]:
    errors: list[str] = []

    def is_placeholder(value: str) -> bool:
        return bool(PLACEHOLDER_RE.match(value)) or "changeme" in value.lower() or "placeholder" in value.lower()

    for section, key in REQUIRED_SCALARS:
        value = get(data, section, key)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(f"{section}.{key}: required value is missing or empty")
            continue
        if not isinstance(value, str):
            errors.append(f"{section}.{key}: expected a string")
            continue
        if "*" in value:
            errors.append(f"{section}.{key}: wildcard identifiers are forbidden")
        if mode == "apply" and is_placeholder(value):
            errors.append(f"{section}.{key}: placeholder value is rejected in apply mode")

    # Identity separation.
    api_sa = get(data, "identities", "api_service_account")
    worker_sa = get(data, "identities", "worker_service_account")
    gateway_sa = get(data, "identities", "gateway_identity")
    if api_sa and worker_sa and api_sa == worker_sa:
        errors.append("identities: API and worker must use different service accounts")
    if gateway_sa and worker_sa and gateway_sa == worker_sa:
        errors.append("identities: gateway and worker must use different identities")

    # CORS origins.
    origins = get(data, "cors", "allowed_origins")
    if not isinstance(origins, list) or not origins:
        errors.append("cors.allowed_origins: at least one explicit origin is required")
    else:
        for origin in origins:
            if not isinstance(origin, str) or not origin.strip():
                errors.append("cors.allowed_origins: empty origin entry")
            elif "*" in origin:
                errors.append(f"cors.allowed_origins: wildcard origin is forbidden: {origin}")
            elif mode == "apply" and is_placeholder(origin):
                errors.append(f"cors.allowed_origins: placeholder origin rejected in apply mode: {origin}")

    # Immutable release identity.
    for key in ("sha", "rollback_sha"):
        value = get(data, "release", key)
        if isinstance(value, str) and value.strip():
            if value.strip().lower() in MUTABLE_TAGS:
                errors.append(f"release.{key}: mutable image tag '{value}' is forbidden")
            elif mode == "apply" and not FULL_SHA_RE.match(value.strip()):
                errors.append(f"release.{key}: must be a full 40-character commit SHA in apply mode")

    # Budgets.
    budgets = get(data, "budgets")
    if not isinstance(budgets, list) or not budgets:
        errors.append("budgets: the list of budget variable names is required")

    # Execution flags: must all exist and be false.
    flags = get(data, "execution_flags")
    if not isinstance(flags, dict):
        errors.append("execution_flags: required mapping is missing")
    else:
        for flag in EXECUTION_FLAGS:
            value = flags.get(flag)
            if value is None:
                errors.append(f"execution_flags.{flag}: required flag entry is missing")
            elif value is not False:
                errors.append(f"execution_flags.{flag}: must be false during Stage A (no manifest may enable execution)")

    # Secrets: names only, single-purpose consumers, no secret material.
    secrets = get(data, "secrets")
    if not isinstance(secrets, dict) or not secrets:
        errors.append("secrets: the mapping of secret resource names is required")
    else:
        for label, entry in secrets.items():
            if not isinstance(entry, dict):
                errors.append(f"secrets.{label}: expected a mapping with name/consumers")
                continue
            name = entry.get("name")
            consumers = entry.get("consumers")
            if not isinstance(name, str) or not name.strip():
                errors.append(f"secrets.{label}.name: required")
            elif SECRET_MATERIAL_RE.search(name):
                errors.append(f"secrets.{label}.name: looks like secret MATERIAL, not a resource name")
            elif mode == "apply" and is_placeholder(name):
                errors.append(f"secrets.{label}.name: placeholder rejected in apply mode")
            if not isinstance(consumers, list) or not consumers:
                errors.append(f"secrets.{label}.consumers: required non-empty list")
            elif not set(consumers) <= {"api", "worker", "gateway"}:
                errors.append(f"secrets.{label}.consumers: entries must be among api/worker/gateway")
        provider = secrets.get("provider_api_key")
        if isinstance(provider, dict) and provider.get("consumers") not in (None, ["worker"]):
            errors.append("secrets.provider_api_key.consumers: the provider key is worker-only")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--mode", choices=["plan", "apply"], default="plan")
    parser.add_argument(
        "--emit-secret-consumers",
        action="store_true",
        help=(
            "Print `name=email[,email]` Secret Manager expectations derived "
            "from the manifest (concrete entries only) and exit; used by "
            "production-readiness.sh to drive check-secret-metadata.sh."
        ),
    )
    args = parser.parse_args()

    path = Path(args.manifest)
    if not path.is_file():
        print(f"BLOCKED manifest not found: {path}", file=sys.stderr)
        return 1
    try:
        data = parse_manifest(path.read_text(encoding="utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"BLOCKED manifest parse error: {exc}", file=sys.stderr)
        return 1

    if args.emit_secret_consumers:
        for line in emit_secret_consumers(data):
            print(line)
        return 0

    errors = validate(data, args.mode)
    if errors:
        print("manifest validation FAILED:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"manifest validation passed ({args.mode} mode): {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
