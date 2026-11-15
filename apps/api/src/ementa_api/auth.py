"""Who is calling the API.

Interface fixed for F3: routes depend on `current_user` and `require_roles`. Until JWT lands,
every request runs as the development user below.
"""

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, HTTPException

from ementa.domain.access import Role

__all__ = ["DEV_USER", "Role", "User", "current_user", "require_roles"]


@dataclass(frozen=True)
class User:
    id: str
    name: str
    role: Role


DEV_USER = User(id="dev", name="Desenvolvimento", role=Role.SOCIA)


def current_user() -> User:
    return DEV_USER


def require_roles(*roles: Role) -> Callable[[User], User]:
    """Dependency that lets only `roles` through; anyone else gets 403."""

    def check(user: User = Depends(current_user)) -> User:  # noqa: B008
        if user.role not in roles:
            raise HTTPException(status_code=403, detail=f"Role {user.role} may not use this route.")
        return user

    return check
