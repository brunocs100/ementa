"""Who is calling the API: OAuth2 password flow with short-lived JWTs (HS256).

`POST /v1/auth/token` checks the password in `firm.users` and hands out a bearer token that
lasts `jwt_ttl_minutes` (15 by default). The other /v1 routes read only the token, never the
database, so a role change takes effect when the token expires.

A route depends on `authorized_user`, which asks the permission matrix
(`ementa.domain.access`) about the route being called, or on `require_roles(...)`. Both answer
401 without a valid token and 403 when the role may not pass, as RFC 9457 problems.
"""

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.security import OAuth2PasswordBearer

from ementa.domain.access import Role, can_call_route
from ementa_kit.config import Environment, Settings, get_settings
from ementa_kit.telemetry import get_logger

__all__ = [
    "Role",
    "User",
    "authorized_user",
    "check_jwt_settings",
    "create_access_token",
    "current_user",
    "require_roles",
    "unauthorized",
]

ALGORITHM = "HS256"
ISSUER = "ementa"
TOKEN_URL = "/v1/auth/token"

bearer = OAuth2PasswordBearer(tokenUrl=TOKEN_URL, auto_error=False)


@dataclass(frozen=True)
class User:
    id: str
    name: str
    role: Role


def check_jwt_settings(settings: Settings) -> None:
    """Outside dev and ci the tokens must outlive a restart and nobody may forge them from the
    code: no configured secret, no API."""
    if settings.jwt_secret is None and settings.env not in (Environment.DEV, Environment.CI):
        raise RuntimeError(
            f"EMENTA_JWT_SECRET is required with EMENTA_ENV={settings.env.value}: set a random "
            "value of at least 32 characters, e.g. from "
            '`python -c "import secrets; print(secrets.token_urlsafe(48))"`.'
        )


@lru_cache
def signing_key() -> str:
    settings = get_settings()
    check_jwt_settings(settings)
    if settings.jwt_secret is not None:
        return settings.jwt_secret.get_secret_value()
    get_logger(__name__).warning(
        "jwt_secret_not_set", detail="random secret for this process; its tokens die with it"
    )
    return secrets.token_urlsafe(48)


def create_access_token(user: User) -> str:
    now = datetime.now(UTC)
    claims = {
        "iss": ISSUER,
        "sub": user.id,
        "name": user.name,
        "role": user.role.value,
        "iat": now,
        "exp": now + timedelta(minutes=get_settings().jwt_ttl_minutes),
    }
    return jwt.encode(claims, signing_key(), algorithm=ALGORITHM)


def unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def current_user(request: Request, token: Annotated[str | None, Depends(bearer)]) -> User:
    """The caller, from the bearer token. Kept in `request.state.user` for the audit trail."""
    if not token:
        raise unauthorized("Send a bearer token from POST /v1/auth/token.")
    try:
        claims = jwt.decode(
            token,
            signing_key(),
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            options={"require": ["exp", "iat", "iss", "sub", "name", "role"]},
        )
        user = User(id=claims["sub"], name=str(claims["name"]), role=Role(claims["role"]))
    except jwt.ExpiredSignatureError:
        raise unauthorized("The token has expired; get a new one at POST /v1/auth/token.") from None
    except (jwt.InvalidTokenError, ValueError):
        raise unauthorized("The token is not valid.") from None
    request.state.user = user
    return user


def require_roles(*roles: Role) -> Callable[[User], User]:
    """Dependency that lets only `roles` through; anyone else gets 403."""
    allowed = frozenset(roles)

    def check(user: Annotated[User, Depends(current_user)]) -> User:
        if user.role not in allowed:
            raise HTTPException(status_code=403, detail=f"Role {user.role} may not use this route.")
        return user

    return check


def authorized_user(request: Request, user: Annotated[User, Depends(current_user)]) -> User:
    """The caller, when the permission matrix grants their role the route being called; 403
    otherwise. A route missing from the matrix is closed to everyone."""
    route = request.scope.get("route")
    key = f"{request.method} {route.path}" if isinstance(route, APIRoute) else request.method
    if not can_call_route(user.role, key):
        raise HTTPException(status_code=403, detail=f"Role {user.role} may not use {key}.")
    return user
