from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Header

from backend.errors import AppError
from backend.gateway_auth import GatewayIdentity, get_verified_gateway


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: UUID
    email: str | None = None
    gateway: GatewayIdentity | None = None


def get_authenticated_user(
    x_milo_auth_user_id: str | None = Header(default=None),
    x_milo_auth_user_email: str | None = Header(default=None),
    gateway: GatewayIdentity | None = Depends(get_verified_gateway),
) -> AuthenticatedUser:
    """Trusted browser identity.

    The internal identity headers are only accepted after the caller proves
    it IS the trusted gateway via a verified Google-signed identity token
    (backend/gateway_auth.py). Merely being able to invoke the private
    Cloud Run service — e.g. as the worker service account — is never
    sufficient. ``gateway`` is None only in explicit non-production
    development mode; production fails closed before this point.
    """
    if not x_milo_auth_user_id:
        raise AppError("AUTHENTICATION_REQUIRED", "authenticated user header is required", 401)
    try:
        user_id = UUID(x_milo_auth_user_id)
    except ValueError as exc:
        raise AppError("AUTHENTICATION_REQUIRED", "authenticated user header is invalid", 401) from exc
    return AuthenticatedUser(user_id=user_id, email=x_milo_auth_user_email, gateway=gateway)
