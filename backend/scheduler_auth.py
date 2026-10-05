"""PR-SYNC-2: the register sync's Cloud Scheduler tick -- ONE caller.

Cloud Scheduler calls the private API with a Google-signed OIDC token minted
for the dedicated service account `milo-register-scheduler@<project>` (no
keys), audience `MILO_GATEWAY_AUDIENCE` (the API service URL). Cloud Run's
IAM admits it (`roles/run.invoker` on the API only) and forwards the
`Authorization: Bearer` header unchanged; the API then verifies it with the
gateway's own verifier: signature (Google certificates), issuer, audience,
expiry, a verified email, and that email equal to
`MILO_REGISTER_SCHEDULER_IDENTITY` -- set on the API by
`scripts/ops/deploy.sh`, never by hand.

Missing or partial configuration, or a scheduler identity that is also a
gateway or worker identity, is 503 and nothing runs. Any other caller --
the gateway's own token included -- is 401. The route is in no gateway
allowlist, so a browser can never reach it.
"""

from __future__ import annotations

import os

from fastapi import Depends, Header

from backend.errors import AppError
from backend.gateway_auth import _approved_gateway_identities, get_gateway_token_verifier
from backend.worker_auth import GOOGLE_ISSUERS, TokenVerifier, _approved_identities

SCHEDULER_IDENTITY_ENV = "MILO_REGISTER_SCHEDULER_IDENTITY"


def _rejected() -> AppError:
    return AppError("SCHEDULER_AUTH_INVALID", "scheduler identity token rejected", 401)


def verify_scheduler_token(authorization: str | None, verifier: TokenVerifier) -> str:
    """The verified scheduler email, or an AppError (503 / 401)."""
    audience = os.getenv("MILO_GATEWAY_AUDIENCE", "").strip()
    identity = os.getenv(SCHEDULER_IDENTITY_ENV, "").strip().lower()
    if not audience or not identity or identity in _approved_gateway_identities() \
            or identity in _approved_identities():
        raise AppError("SCHEDULER_AUTH_NOT_CONFIGURED", "the register scheduler is not configured", 503)
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _rejected()
    try:
        claims = verifier.verify(token.strip(), audience)
    except Exception as exc:  # signature, expiry, audience, format failures
        raise _rejected() from exc
    if str(claims.get("iss", "")) not in GOOGLE_ISSUERS or str(claims.get("aud", "")) != audience:
        raise _rejected()
    email = str(claims.get("email", "")).strip().lower()
    if claims.get("email_verified") is not True or email != identity:
        raise _rejected()
    return email


def get_verified_scheduler(authorization: str | None = Header(default=None),
                           verifier: TokenVerifier = Depends(get_gateway_token_verifier)) -> str:
    return verify_scheduler_token(authorization, verifier)
