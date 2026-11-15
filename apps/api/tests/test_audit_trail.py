import os
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from psycopg import sql

from ementa.agents.legal_research import ResearchAnswer
from ementa.users import FirmUser, UserDirectory, seed_demo_users
from ementa_api.app import create_app
from ementa_api.auth import Role, User, create_access_token
from ementa_kit.audit import AuditEvent, AuditLog, MemoryAuditLog, PgAuditLog, canonical

QUESTION = "Meu CPF é 123.456.789-09 e fui negativado sem aviso. Posso pedir indenização?"
CPF, CPF_DIGITS = "123.456.789-09", "12345678909"


class Canned:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def ask(self, question: str) -> ResearchAnswer:
        if self.error:
            raise self.error
        return ResearchAnswer(
            question=question,
            answer="Cabe indenização por dano moral [stj:sumula:385].",
            citations=[],
            removed=[],
            sources=["stj:sumula:385"],
            queries=[question],
            attempts=1,
            prompt="legal_research.answer@1",
            model="fake-model",
        )


class OneUser:
    def authenticate(self, username: str, password: str) -> FirmUser | None:
        if (username, password) == ("socia", "socia-demo"):
            return FirmUser("id-socia", "socia", "Helena Duarte", Role.SOCIA)
        return None


def build(log: AuditLog, researcher: Canned | None = None) -> FastAPI:
    fake = researcher or Canned()
    return create_app(researcher=lambda: fake, chunks=lambda: 1, users=OneUser, audit=lambda: log)


def bearer(role: Role) -> dict[str, str]:
    token = create_access_token(User(id=f"id-{role}", name="Pessoa de Teste", role=role))
    return {"Authorization": f"Bearer {token}"}


def events(log: MemoryAuditLog) -> list[AuditEvent]:
    return [record.event for record in log.records]


def test_each_v1_request_leaves_one_event_with_actor_route_status_and_duration() -> None:
    log = MemoryAuditLog()

    response = TestClient(build(log)).post(
        "/v1/legal-research", json={"question": QUESTION}, headers=bearer(Role.ASSOCIADO)
    )

    assert response.status_code == 200
    [event] = events(log)
    assert (event.actor, event.action, event.resource) == (
        "id-associado",
        "http.request",
        "POST /v1/legal-research",
    )
    assert (event.data_class, event.outcome) == ("public", "ok")
    assert event.details["status"] == 200
    assert isinstance(event.details["duration_ms"], int)
    assert set(event.details) == {"status", "duration_ms"}
    assert log.verify().intact


def test_events_hold_no_body_query_or_path_value() -> None:
    log = MemoryAuditLog()
    http = TestClient(build(log), headers=bearer(Role.SOCIA))

    http.post(f"/v1/legal-research?cpf={CPF}", json={"question": QUESTION})
    http.get(f"/v1/clients/{CPF_DIGITS}")
    http.get(f"/v1/audit/events?cursor={CPF_DIGITS}")

    assert [e.resource for e in events(log)] == [
        "POST /v1/legal-research",
        "GET (no route)",
        "GET /v1/audit/events",
    ]
    for record in log.records:
        assert CPF not in canonical(record.event).decode()
        assert CPF_DIGITS not in canonical(record.event).decode()
        assert "indenização" not in canonical(record.event).decode()


def test_refused_invalid_and_failed_requests_are_recorded() -> None:
    log = MemoryAuditLog()
    http = TestClient(build(log, Canned(RuntimeError("boom"))), raise_server_exceptions=False)

    http.post("/v1/legal-research", json={"question": QUESTION})
    http.get("/v1/audit/events", headers=bearer(Role.ESTAGIARIO))
    http.post("/v1/legal-research", json={}, headers=bearer(Role.SOCIA))
    http.post("/v1/legal-research", json={"question": QUESTION}, headers=bearer(Role.SOCIA))

    assert [(e.actor, e.outcome, e.details["status"]) for e in events(log)] == [
        (None, "denied", 401),
        ("id-estagiario", "denied", 403),
        ("id-socia", "rejected", 422),
        ("id-socia", "error", 500),
    ]


def test_the_login_event_names_who_logged_in_and_never_the_password() -> None:
    log = MemoryAuditLog()
    http = TestClient(build(log))

    http.post("/v1/auth/token", data={"username": "socia", "password": "socia-demo"})
    http.post("/v1/auth/token", data={"username": "socia", "password": "chute"})

    assert [(e.actor, e.resource, e.outcome) for e in events(log)] == [
        ("id-socia", "POST /v1/auth/token", "ok"),
        (None, "POST /v1/auth/token", "denied"),
    ]
    assert all(e.data_class == "restricted" for e in events(log))
    assert not any(b"socia-demo" in canonical(e) or b"chute" in canonical(e) for e in events(log))


def test_routes_outside_v1_are_not_audited() -> None:
    log = MemoryAuditLog()
    http = TestClient(build(log))

    for path in ("/healthz", "/readyz", "/docs", "/openapi.json"):
        assert http.get(path).status_code == 200
    assert log.records == []


def test_a_streamed_response_is_recorded_when_it_ends() -> None:
    log = MemoryAuditLog()
    app = build(log)

    @app.get("/v1/stream")
    def stream() -> StreamingResponse:
        return StreamingResponse(
            iter(["data: a\n\n", "data: b\n\n"]), media_type="text/event-stream"
        )

    assert TestClient(app).get("/v1/stream").text == "data: a\n\ndata: b\n\n"
    [event] = events(log)
    assert (event.resource, event.details["status"]) == ("GET /v1/stream", 200)


class BrokenLog(MemoryAuditLog):
    def append(self, event: AuditEvent) -> Any:
        raise psycopg.OperationalError("connection refused")


def test_a_failed_audit_write_does_not_fail_the_request() -> None:
    response = TestClient(build(BrokenLog())).post(
        "/v1/legal-research", json={"question": QUESTION}, headers=bearer(Role.SOCIA)
    )

    assert response.status_code == 200


@pytest.mark.parametrize("role", list(Role))
def test_only_socia_dpo_and_ti_read_the_trail(role: Role) -> None:
    response = TestClient(build(MemoryAuditLog())).get("/v1/audit/events", headers=bearer(role))

    assert response.status_code == (200 if role in {Role.SOCIA, Role.DPO, Role.TI} else 403)
    if response.status_code == 403:
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["detail"] == f"Role {role} may not use GET /v1/audit/events."


def test_the_trail_pages_newest_first_with_a_cursor() -> None:
    log = MemoryAuditLog()
    for n in range(5):
        log.append(AuditEvent(f"u-{n}", "tool.call", "calculate_deadline", "public", "ok"))
    http = TestClient(build(log), headers=bearer(Role.DPO))

    first = http.get("/v1/audit/events", params={"limit": 2}).json()
    second = http.get("/v1/audit/events", params={"limit": 2, "cursor": first["next_cursor"]})
    last = http.get("/v1/audit/events", params={"limit": 2, "cursor": "2"}).json()

    assert [item["id"] for item in first["items"]] == [5, 4]
    assert first["next_cursor"] == "4"
    assert [item["id"] for item in second.json()["items"]] == [3, 2]
    assert ([item["id"] for item in last["items"]], last["next_cursor"]) == ([1], None)
    newest, older = first["items"]
    assert newest["prev_hash"] == older["hash"]
    assert newest["actor"] == "u-4"
    assert set(newest) == {
        "id",
        "occurred_at",
        "actor",
        "action",
        "resource",
        "data_class",
        "outcome",
        "details",
        "prev_hash",
        "hash",
    }


@pytest.mark.parametrize(
    "params", [{"cursor": "abc"}, {"cursor": "0"}, {"cursor": "-1"}, {"limit": 0}, {"limit": 201}]
)
def test_a_bad_page_request_is_a_422_problem(params: dict[str, Any]) -> None:
    http = TestClient(build(MemoryAuditLog()), headers=bearer(Role.TI))

    response = http.get("/v1/audit/events", params=params)

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"


DATABASE_URL = os.environ.get("EMENTA_TEST_DATABASE_URL")


@pytest.fixture
def postgres() -> Iterator[tuple[UserDirectory, PgAuditLog]]:
    if not DATABASE_URL:
        pytest.skip("EMENTA_TEST_DATABASE_URL not set")
    suffix = uuid.uuid4().hex[:8]
    directory = UserDirectory(DATABASE_URL, schema=f"test_firm_{suffix}")
    log = PgAuditLog(DATABASE_URL, schema=f"test_audit_{suffix}")
    directory.ensure_schema()
    log.ensure_schema()
    yield directory, log
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        for schema in (directory.schema, log.schema):
            conn.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(schema)))


@pytest.mark.integration
def test_login_research_and_audit_read_on_postgres(
    postgres: tuple[UserDirectory, PgAuditLog],
) -> None:
    directory, log = postgres
    ids = {user.username: user.id for user in seed_demo_users(directory)}
    app = create_app(
        researcher=Canned, chunks=lambda: 1, users=lambda: directory, audit=lambda: log
    )
    http = TestClient(app)

    def login(username: str) -> dict[str, str]:
        form = {"username": username, "password": f"{username}-demo"}
        token = http.post("/v1/auth/token", data=form).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    estagiario = login("estagiario")
    assert http.post(
        "/v1/legal-research", json={"question": QUESTION}, headers=estagiario
    ).is_success
    assert http.get("/v1/audit/events", headers=estagiario).status_code == 403
    trail = http.get("/v1/audit/events", headers=login("dpo")).json()["items"]

    assert [(e["actor"], e["resource"], e["outcome"]) for e in trail] == [
        (ids["dpo"], "POST /v1/auth/token", "ok"),
        (ids["estagiario"], "GET /v1/audit/events", "denied"),
        (ids["estagiario"], "POST /v1/legal-research", "ok"),
        (ids["estagiario"], "POST /v1/auth/token", "ok"),
    ]
    check = log.verify()
    assert check.intact
    assert check.checked == 5  # the DPO's read is recorded after its response
