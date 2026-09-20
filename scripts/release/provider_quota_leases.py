#!/usr/bin/env python3
"""Operator tool: see, and deliberately recover, held provider concurrency leases.

Why this exists
---------------

``backend/provider_quota.py`` returns a unit of organization inference
concurrency to the shared pool ONLY when MILO can prove the request that took
it is over. Every other outcome -- a fired deadline, a transport error, an
exception whose text merely resembles a 429, a worker killed mid-request --
leaves the lease HELD, with no expiry, because Kimi publishes no
cancellation-on-disconnect guarantee and no maximum provider-side lifetime for
an abandoned request. MILO cannot prove when the provider stops counting such
a request, so in production it never frees the slot on a timer (the opt-in
timer is refused there).

Held leases therefore accumulate on failure paths, and each one reduces the
capacity MILO will use. This tool is the ONLY way one leaves the store without
proof of completion, and it is deliberately narrow:

* it is never called by MILO itself -- not by the scheduler, not by
  acquisition, not by a background thread, not by a deployment hook;
* ``list`` is read-only;
* ``recover`` removes ONE lease per invocation, by id, and requires a recorded
  justification plus an explicit attestation flag;
* it refuses a lease younger than the process-lifetime floor (the deployed
  worker task timeout plus margin, 4500s), because that lease's own process
  may still be alive and may still settle it -- the check and the removal are
  one atomic store operation -- and a lease whose age is unknown;
* past the horizon MILO can say only that the MILO process is gone. Whether
  the provider has stopped counting the request is the operator's judgement,
  which is exactly what the justification records.

Before recovering, an operator should have checked the provider console for
in-flight requests (or accepted, in the justification, that the request is
older than any plausible provider-side lifetime). This tool cannot check that
for you and does not claim to.

Usage::

    python3 scripts/release/provider_quota_leases.py list
    python3 scripts/release/provider_quota_leases.py recover \\
        --lease-id <hex> \\
        --justification "console shows 0 in-flight; worker task killed 2h ago" \\
        --i-have-verified-provider-side-completion

Environment: ``UPSTASH_REDIS_REST_URL``, ``UPSTASH_REDIS_REST_TOKEN`` (the
worker's own shared store; there is no in-memory fallback, because a lease
that is not in the shared store does not exist), and optionally
``MILO_PROVIDER_QUOTA_SCOPE`` / ``MILO_WORKER_MAX_LIFETIME_SECONDS`` exactly as
the worker reads them. The token is never printed.

Exit codes: 0 done; 2 refused (not held, too young, no justification, no
attestation); 1 store unreachable or configuration invalid.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any, Callable, Mapping

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from backend.provider_quota import (LeaseRecoveryRefused, ProviderQuotaCoordinator,  # noqa: E402
                                    ProviderQuotaUnavailable, QuotaBackend, QuotaConfig,
                                    UpstashQuotaBackend)

ATTEST_FLAG = "--i-have-verified-provider-side-completion"

BackendFactory = Callable[[Mapping[str, str]], QuotaBackend]


def _shared_store(env: Mapping[str, str]) -> QuotaBackend:
    url = (env.get("UPSTASH_REDIS_REST_URL") or "").strip()
    token = (env.get("UPSTASH_REDIS_REST_TOKEN") or "").strip()
    if not url or not token:
        raise ProviderQuotaUnavailable(
            "UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN are required: this tool "
            "acts on the worker's shared store only")
    return UpstashQuotaBackend(url, token)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC).isoformat(timespec="seconds")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect held provider concurrency leases, or recover ONE deliberately.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show every held lease (read-only)")
    recover = sub.add_parser("recover", help="remove ONE held lease by id, with a recorded reason")
    recover.add_argument("--lease-id", required=True)
    recover.add_argument("--justification", required=True,
                         help="why you are sure the provider is no longer counting this request")
    recover.add_argument(ATTEST_FLAG, dest="attested", action="store_true",
                         help="required: you have checked provider-side completion yourself; "
                              "MILO cannot check it for you")
    return parser


def main(argv: list[str] | None = None, *, env: Mapping[str, str] | None = None,
         backend_factory: BackendFactory = _shared_store,
         out: Any = sys.stdout, err: Any = sys.stderr) -> int:
    args = build_parser().parse_args(argv)
    source = dict(os.environ if env is None else env)
    events: list[dict[str, Any]] = []
    try:
        config = QuotaConfig.from_env(source)
        coordinator = ProviderQuotaCoordinator(
            backend_factory(source), config,
            diagnostic_sink=lambda kind, payload: events.append({"event": kind, **payload}))
        if args.command == "list":
            held = coordinator.held_inference_leases()
            print(json.dumps({
                "scope": config.scope,
                "guarantee": config.concurrency_guarantee,
                "minimum_age_for_reclaim_seconds": config.minimum_abandoned_lease_reclaim_seconds,
                "held": len(held),
                "leases": [{"lease_id": item.lease_id,
                            "acquired_at": _iso(item.acquired_at_ms) if item.acquired_at_ms else None,
                            "age_seconds": item.age_seconds,
                            "expires": None if item.expires_at == float("inf") else item.expires_at,
                            "recovery_eligible": item.recovery_eligible} for item in held],
            }, indent=2), file=out)
            return 0
        if not args.attested:
            print(json.dumps({"refused": "ATTESTATION_REQUIRED", "flag": ATTEST_FLAG}), file=err)
            return 2
        try:
            coordinator.operator_reclaim_inference(args.lease_id, reason=args.justification)
        except LeaseRecoveryRefused as refusal:
            print(json.dumps({"refused": refusal.code, "detail": str(refusal)}), file=err)
            return 2
        print(json.dumps({"recovered": args.lease_id, "events": events}, indent=2), file=out)
        return 0
    except ProviderQuotaUnavailable as exc:
        # Static code + detail only. The store URL and token never print.
        print(json.dumps({"error": exc.code, "detail": exc.message}), file=err)
        return 1
    except ValueError as exc:
        print(json.dumps({"error": "CONFIG_INVALID", "detail": str(exc)}), file=err)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
