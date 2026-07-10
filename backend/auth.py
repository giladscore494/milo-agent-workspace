from dataclasses import dataclass
from uuid import UUID

from fastapi import Header

from backend.errors import AppError


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: UUID
    email: str | None = None


def get_authenticated_user(
    x_milo_auth_user_id: str | None = Header(default=None),
    x_milo_auth_user_email: str | None = Header(default=None),
) -> AuthenticatedUser:
    if not x_milo_auth_user_id:
        raise AppError("AUTHENTICATION_REQUIRED", "authenticated user header is required", 401)
    try:
        user_id = UUID(x_milo_auth_user_id)
    except ValueError as exc:
        raise AppError("AUTHENTICATION_REQUIRED", "authenticated user header is invalid", 401) from exc
    return AuthenticatedUser(user_id=user_id, email=x_milo_auth_user_email)
