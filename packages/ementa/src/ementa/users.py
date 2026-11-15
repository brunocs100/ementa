"""Users of the firm in `firm.users`, with scrypt password hashes.

`uv run ementa seed-users` writes `DEMO_USERS`, one fictitious person per role; their passwords
are in docs/SEGURANCA-E-GOVERNANCA.md. Nobody here is real.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from functools import cache
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from ementa.domain.access import Role

# scrypt with one of the OWASP parameter sets: N = 2^14, r = 8, p = 5 (16 MiB, about 0.2 s).
_N, _R, _P = 2**14, 8, 5
_MAXMEM = 64 * 1024 * 1024  # also caps the parameters read back from a stored hash


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM, dklen=32
    )
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = encoded.split("$")
        expected = bytes.fromhex(digest)
        if scheme != "scrypt" or not expected:
            return False
        actual = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_MAXMEM,
            dklen=len(expected),
        )
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


@cache
def _decoy_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))


@dataclass(frozen=True)
class FirmUser:
    id: str
    username: str
    name: str
    role: Role


@dataclass(frozen=True)
class DemoUser:
    username: str
    name: str
    role: Role
    password: str


DEMO_USERS = (
    DemoUser("socia", "Helena Duarte", Role.SOCIA, "socia-demo"),
    DemoUser("associado", "Rafael Moreira", Role.ASSOCIADO, "associado-demo"),
    DemoUser("estagiario", "Lucas Ferreira", Role.ESTAGIARIO, "estagiario-demo"),
    DemoUser("controladoria", "Beatriz Santos", Role.CONTROLADORIA, "controladoria-demo"),
    DemoUser("dpo", "Camila Rocha", Role.DPO, "dpo-demo"),
    DemoUser("ti", "Diego Almeida", Role.TI, "ti-demo"),
)
"""Fictitious people for the demo, one per role. The passwords are public on purpose."""

_SCHEMA = """
create schema if not exists {schema};
create table if not exists {schema}.users (
    id uuid primary key default gen_random_uuid(),
    username text not null unique,
    name text not null,
    role text not null check (role in ({roles})),
    password_hash text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
"""


def _user(row: dict[str, Any]) -> FirmUser:
    return FirmUser(str(row["id"]), row["username"], row["name"], Role(row["role"]))


class UserDirectory:
    def __init__(self, database_url: str, schema: str = "firm") -> None:
        self.database_url = database_url
        self.schema = schema

    def _sql(self, template: str) -> sql.Composed:
        return sql.SQL(template).format(
            schema=sql.Identifier(self.schema),
            roles=sql.SQL(", ").join(sql.Literal(role.value) for role in Role),
        )

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(self.database_url, autocommit=True, row_factory=dict_row)

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(self._sql(_SCHEMA))

    def save(self, username: str, name: str, role: Role, password: str) -> FirmUser:
        """Create the user, or update name, role and password; the id stays."""
        with self._connect() as conn:
            row = conn.execute(
                self._sql(
                    "insert into {schema}.users (username, name, role, password_hash) "
                    "values (%s, %s, %s, %s) on conflict (username) do update set "
                    "name = excluded.name, role = excluded.role, "
                    "password_hash = excluded.password_hash, updated_at = now() "
                    "returning id, username, name, role"
                ),
                (username, name, role.value, hash_password(password)),
            ).fetchone()
        assert row is not None
        return _user(row)

    def authenticate(self, username: str, password: str) -> FirmUser | None:
        """The user, when the password matches. An unknown username costs the same scrypt as a
        wrong password, so the response time does not tell who has an account."""
        with self._connect() as conn:
            row = conn.execute(
                self._sql(
                    "select id, username, name, role, password_hash from {schema}.users "
                    "where username = %s"
                ),
                (username,),
            ).fetchone()
        if row is None:
            verify_password(password, _decoy_hash())
            return None
        return _user(row) if verify_password(password, row["password_hash"]) else None


def seed_demo_users(directory: UserDirectory) -> list[FirmUser]:
    """Write `DEMO_USERS`; running it again updates them and keeps their ids."""
    return [directory.save(u.username, u.name, u.role, u.password) for u in DEMO_USERS]
