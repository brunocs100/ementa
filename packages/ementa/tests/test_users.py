import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from ementa import cli, services
from ementa.domain.access import Role
from ementa.users import (
    DEMO_USERS,
    UserDirectory,
    hash_password,
    seed_demo_users,
    verify_password,
)
from ementa_kit.config import Environment, Settings

DATABASE_URL = os.environ.get("EMENTA_TEST_DATABASE_URL")


def test_password_hash_is_salted_scrypt_and_verifies() -> None:
    first, second = hash_password("socia-demo"), hash_password("socia-demo")

    assert first.startswith("scrypt$16384$8$5$")
    assert first != second  # a fresh salt each time
    assert verify_password("socia-demo", first)
    assert not verify_password("socia-dem0", first)
    assert "socia-demo" not in first


@pytest.mark.parametrize(
    "stored",
    ["", "socia-demo", "bcrypt$1$2$3$00$00", "scrypt$16384$8$5$zz$00", "scrypt$16384$8$5$00$"],
)
def test_a_malformed_hash_never_verifies(stored: str) -> None:
    assert not verify_password("socia-demo", stored)


def test_demo_users_cover_every_role_once() -> None:
    assert sorted(u.role for u in DEMO_USERS) == sorted(Role)
    assert len({u.username for u in DEMO_USERS}) == len(DEMO_USERS)


def test_seed_users_refuses_to_run_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    prod = Settings(_env_file=None, env=Environment.PROD)  # type: ignore[call-arg]
    monkeypatch.setattr(cli, "get_settings", lambda: prod)
    monkeypatch.setattr(cli, "setup", lambda settings: None)
    monkeypatch.setattr(services, "user_directory", lambda: pytest.fail("touched the database"))

    assert cli.main(["seed-users"]) == 2


@pytest.fixture
def directory() -> Iterator[UserDirectory]:
    if not DATABASE_URL:
        pytest.skip("EMENTA_TEST_DATABASE_URL not set")
    directory = UserDirectory(DATABASE_URL, schema=f"test_firm_{uuid.uuid4().hex[:8]}")
    directory.ensure_schema()
    directory.ensure_schema()  # idempotent
    yield directory
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(directory.schema)))


@pytest.mark.integration
def test_seed_is_idempotent_and_keeps_the_ids(directory: UserDirectory) -> None:
    first = seed_demo_users(directory)
    again = seed_demo_users(directory)

    assert [u.id for u in again] == [u.id for u in first]
    assert {u.role for u in again} == set(Role)
    assert directory.authenticate("dpo", "dpo-demo") == next(u for u in first if u.role == "dpo")


@pytest.mark.integration
def test_authenticate_needs_the_right_password_of_a_known_user(directory: UserDirectory) -> None:
    saved = directory.save("socia", "Helena Duarte", Role.SOCIA, "socia-demo")

    assert directory.authenticate("socia", "socia-demo") == saved
    assert directory.authenticate("socia", "errada") is None
    assert directory.authenticate("ninguem", "socia-demo") is None
    assert directory.authenticate("SOCIA", "socia-demo") is None


@pytest.mark.integration
def test_saving_again_changes_role_and_password(directory: UserDirectory) -> None:
    before = directory.save("lucas", "Lucas Ferreira", Role.ESTAGIARIO, "antiga")
    after = directory.save("lucas", "Lucas Ferreira", Role.ASSOCIADO, "nova")

    assert (after.id, after.role) == (before.id, Role.ASSOCIADO)
    assert directory.authenticate("lucas", "antiga") is None
    assert directory.authenticate("lucas", "nova") == after


@pytest.mark.integration
def test_the_table_takes_only_known_roles(directory: UserDirectory) -> None:
    assert DATABASE_URL
    with (
        psycopg.connect(DATABASE_URL, autocommit=True) as conn,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        conn.execute(
            sql.SQL(
                "insert into {}.users (username, name, role, password_hash) "
                "values ('x', 'X', 'cliente', 'h')"
            ).format(sql.Identifier(directory.schema))
        )


@pytest.mark.integration
def test_seed_users_command_writes_the_demo_users(
    directory: UserDirectory, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(_env_file=None))  # type: ignore[call-arg]
    monkeypatch.setattr(cli, "setup", lambda settings: None)
    monkeypatch.setattr(services, "user_directory", lambda: directory)

    assert cli.main(["seed-users"]) == 0
    out = capsys.readouterr().out
    assert "6 fictitious users" in out
    assert "estagiario" in out
    assert "estagiario-demo" not in out
    assert directory.authenticate("estagiario", "estagiario-demo") is not None
