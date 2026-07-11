"""Service-to-service authentication for internal worker mutation routes.

Worker routes never use browser-user authentication. The Cloud Run worker
job obtains a Google-signed OIDC identity token for the configured audience
(``MILO_WORKER_AUDIENCE``) and presents it as ``X-Milo-Worker-Token``. The
API verifies, in order:

1. token signature against Google's published certificates;
2. issuer (``accounts.google.com`` / ``https://accounts.google.com``);
3. audience (must equal ``MILO_WORKER_AUDIENCE`` exactly);
4. expiration;
5. verified service-account identity (``email`` + ``email_verified``);
6. membership of that identity in the explicit allowlist
   ``MILO_APPROVED_WORKER_IDENTITIES`` (comma-separated service-account
   emails; empty means every request fails closed).

Browser-provided identity headers (``x-milo-auth-user-id`` and friends) are
never consulted here, so a browser user is rejected even when
``MILO_ENABLE_EXECUTION_CONTROL`` is enabled: the feature flag alone is
never sufficient authorization. The Supabase access token a browser holds
is signed by Supabase, not Google, so it can never pass signature
verification either.

Required manual IAM configuration (never applied from this repository):
- create a dedicated worker service account (e.g.
  ``milo-worker@PROJECT.iam.gserviceaccount.com``);
- grant it ``roles/run.invoker`` on the private API service only;
- set ``MILO_WORKER_AUDIENCE`` on the API to the API service URL;
- set ``MILO_APPROVED_WORKER_IDENTITIES`` to exactly that service account;
- configure the worker job to mint ID tokens for that audience via its
  metadata server (no key files).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from fastapi import Depends, Header

from backend.errors import AppError

GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
WORKER_TOKEN_HEADER = "X-Milo-Worker-Token"


@dataclass(frozen=True)
class WorkerIdentity:
    service_account_email: str
    audience: str
    issuer: str


class TokenVerifier(Protocol):
    """Verifies signature, audience and expiration, returning the claims."""

    def verify(self, token: str, audience: str) -> dict[str, Any]: ...


class GoogleIdTokenVerifier:
    """Production verifier backed by google-auth certificate validation."""

    def verify(self, token: str, audience: str) -> dict[str, Any]:
        import google.auth.transport.requests
        from google.oauth2 import id_token as google_id_token

        request = google.auth.transport.requests.Request()
        # verify_oauth2_token checks signature, expiration and audience.
        return google_id_token.verify_oauth2_token(token, request, audience=audience)


@lru_cache
def get_token_verifier() -> TokenVerifier:
    return GoogleIdTokenVerifier()


def _approved_identities() -> frozenset[str]:
    raw = os.getenv("MILO_APPROVED_WORKER_IDENTITIES", "")
    return frozenset(item.strip().lower() for item in raw.split(",") if item.strip())


def verify_worker_token(token: str | None, verifier: TokenVerifier) -> WorkerIdentity:
    audience = os.getenv("MILO_WORKER_AUDIENCE", "").strip()
    approved = _approved_identities()
    if not audience or not approved:
        # Fail closed: worker mutations are unusable until the operator
        # configures both the audience and a non-empty identity allowlist.
        raise AppError("WORKER_AUTH_NOT_CONFIGURED", "worker service authentication is not configured", 503)
    if not token:
        raise AppError("WORKER_AUTH_REQUIRED", "verified worker identity token is required", 401)
    try:
        claims = verifier.verify(token, audience)
    except Exception as exc:  # signature, expiry, audience, format failures
        raise AppError("WORKER_AUTH_INVALID", "worker identity token rejected", 401) from exc
    issuer = str(claims.get("iss", ""))
    if issuer not in GOOGLE_ISSUERS:
        raise AppError("WORKER_AUTH_INVALID", "worker identity token rejected", 401)
    if str(claims.get("aud", "")) != audience:
        raise AppError("WORKER_AUTH_INVALID", "worker identity token rejected", 401)
    email = str(claims.get("email", "")).strip().lower()
    if not email or claims.get("email_verified") is not True:
        raise AppError("WORKER_AUTH_INVALID", "worker identity token rejected", 401)
    if email not in approved:
        raise AppError("WORKER_IDENTITY_NOT_APPROVED", "service identity is not approved for worker mutations", 403)
    return WorkerIdentity(service_account_email=email, audience=audience, issuer=issuer)


def get_verified_worker(
    x_milo_worker_token: str | None = Header(default=None),
    verifier: TokenVerifier = Depends(get_token_verifier),
) -> WorkerIdentity:
    return verify_worker_token(x_milo_worker_token, verifier)
