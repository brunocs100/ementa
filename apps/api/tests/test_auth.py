from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ementa.agents.legal_research import ResearchAnswer
from ementa.domain.access import OPEN_ROUTES, PERMISSIONS
from ementa.users import FirmUser
from ementa_api import app as app_module
from ementa_api.app import create_app
from ementa_api.auth import (
    Role,
    User,
    create_access_token,
    current_user,
    require_roles,
    signing_key,
)
from ementa_kit.config import Environment, Settings

QUESTION = "Comprei pela internet e me arrependi. Tenho quanto tempo para desistir?"


class Canned:
    def __init__(self) -> None:
        self.questions: list[str] = []

    def ask(self, question: str) -> ResearchAnswer:
        self.questions.append(question)
        return ResearchAnswer(
            question=question,
            answer="Você tem 7 dias para desistir [cdc:art49].",
            citations=[],
            removed=[],
            sources=["cdc:art49"],
            queries=[question],
            attempts=1,
            prompt="legal_research.answer@1",
            model="fake-model",
        )


ACCOUNTS = {
    "socia": ("socia-demo", FirmUser("id-socia", "socia", "Helena Duarte", Role.SOCIA)),
    "estagiario": (
        "estagiario-demo",
        FirmUser("id-estagiario", "estagiario", "Lucas Ferreira", Role.ESTAGIARIO),
    ),
}


class FakeUsers:
    """Plain-text passwords; the real directory hashes them (tested in packages/ementa)."""

    def authenticate(self, username: str, password: str) -> FirmUser | None:
        account = ACCOUNTS.get(username)
        return account[1] if account and account[0] == password else None


def build(researcher: Canned | None = None) -> FastAPI:
    fake = researcher or Canned()
    return create_app(researcher=lambda: fake, chunks=lambda: 1, users=FakeUsers)


def client(app: FastAPI | None = None) -> TestClient:
    return TestClient(app or build(), raise_server_exceptions=False)


def bearer(role: Role) -> dict[str, str]:
    token = create_access_token(User(id=f"id-{role}", name="Pessoa de Teste", role=role))
    return {"Authorization": f"Bearer {token}"}


def login(http: TestClient, username: str, password: str) -> Any:
    return http.post("/v1/auth/token", data={"username": username, "password": password})


def test_login_hands_out_a_short_lived_bearer_token() -> None:
    response = login(client(), "socia", "socia-demo")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert (body["token_type"], body["expires_in"]) == ("bearer", 900)
    claims = jwt.decode(body["access_token"], signing_key(), algorithms=["HS256"], issuer="ementa")
    assert (claims["sub"], claims["role"], claims["name"]) == ("id-socia", "socia", "Helena Duarte")
    assert claims["exp"] - claims["iat"] == 900


@pytest.mark.parametrize(("username", "password"), [("socia", "errada"), ("ninguem", "x")])
def test_wrong_credentials_are_a_401_problem(username: str, password: str) -> None:
    response = login(client(), username, password)

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"] == "Wrong username or password."
    assert password not in response.text


def test_a_login_form_error_does_not_echo_the_password() -> None:
    response = client().post("/v1/auth/token", data={"password": "segredo-da-socia"})

    assert response.status_code == 422
    assert response.json()["errors"][0]["loc"] == ["body", "username"]
    assert "segredo-da-socia" not in response.text


def test_the_token_from_the_login_opens_the_research_route() -> None:
    researcher = Canned()
    http = client(build(researcher))
    token = login(http, "estagiario", "estagiario-demo").json()["access_token"]

    response = http.post(
        "/v1/legal-research",
        json={"question": QUESTION},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert researcher.questions == [QUESTION]


def test_research_without_a_token_is_a_401_problem_and_never_runs() -> None:
    researcher = Canned()
    response = client(build(researcher)).post("/v1/legal-research", json={"question": QUESTION})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "type": "about:blank",
        "title": "Unauthorized",
        "status": 401,
        "detail": "Send a bearer token from POST /v1/auth/token.",
        "instance": "/v1/legal-research",
    }
    assert researcher.questions == []


def claims(**changes: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    base: dict[str, Any] = {
        "iss": "ementa",
        "sub": "id-socia",
        "name": "Helena Duarte",
        "role": "socia",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    base.update(changes)
    return {k: v for k, v in base.items() if v is not None}


def signed(payload: dict[str, Any], key: str | None = None, algorithm: str = "HS256") -> str:
    return jwt.encode(payload, signing_key() if key is None else key, algorithm=algorithm)


EXPIRED = claims(iat=datetime.now(UTC) - timedelta(minutes=20), exp=datetime.now(UTC))


@pytest.mark.parametrize(
    ("token", "detail"),
    [
        (lambda: signed(EXPIRED), "The token has expired; get a new one at POST /v1/auth/token."),
        (lambda: signed(claims(), key="y" * 32), "The token is not valid."),
        (lambda: signed(claims(), algorithm="HS512"), "The token is not valid."),
        (lambda: signed(claims(), key="", algorithm="none"), "The token is not valid."),
        (lambda: signed(claims(role="cliente")), "The token is not valid."),
        (lambda: signed(claims(role=None)), "The token is not valid."),
        (lambda: signed(claims(iss="outro")), "The token is not valid."),
        (lambda: "abc.def.ghi", "The token is not valid."),
    ],
    ids=["expired", "other-key", "hs512", "alg-none", "unknown-role", "no-role", "issuer", "junk"],
)
def test_a_bad_token_is_a_401_problem(token: Callable[[], str], detail: str) -> None:
    response = client().post(
        "/v1/legal-research",
        json={"question": QUESTION},
        headers={"Authorization": f"Bearer {token()}"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == detail


@pytest.mark.parametrize("role", list(Role))
def test_every_role_may_research(role: Role) -> None:
    response = client().post(
        "/v1/legal-research", json={"question": QUESTION}, headers=bearer(role)
    )

    assert response.status_code == 200


def test_an_estagiario_gets_403_on_a_socia_only_route() -> None:
    app = build()

    @app.get("/v1/socia-only", dependencies=[Depends(require_roles(Role.SOCIA))])
    def socia_only() -> dict[str, str]:
        return {"ok": "sim"}

    http = client(app)
    denied = http.get("/v1/socia-only", headers=bearer(Role.ESTAGIARIO))

    assert denied.status_code == 403
    assert denied.headers["content-type"] == "application/problem+json"
    assert denied.json()["detail"] == "Role estagiario may not use this route."
    assert http.get("/v1/socia-only", headers=bearer(Role.SOCIA)).json() == {"ok": "sim"}
    assert http.get("/v1/socia-only").status_code == 401


def depends_on(dependant: Dependant, call: object) -> bool:
    return dependant.call is call or any(depends_on(d, call) for d in dependant.dependencies)


def test_every_v1_route_needs_a_token_and_has_a_line_in_the_matrix() -> None:
    """A new /v1 route fails here until it depends on `current_user` (through `authorized_user`
    or `require_roles`) and the matrix in `ementa.domain.access` grants it to someone."""
    granted = set().union(*(grants.routes for grants in PERMISSIONS.values()))
    routes = [r for r in build().routes if isinstance(r, APIRoute) and r.path.startswith("/v1/")]
    served = {f"{method} {r.path}" for r in routes for method in r.methods or ()}

    for route in routes:
        for method in route.methods or ():
            key = f"{method} {route.path}"
            if key in OPEN_ROUTES:
                continue
            assert depends_on(route.dependant, current_user), f"{key} answers without a token"
            assert key in granted, f"{key} has no line in the permission matrix"
    assert granted <= served, f"the matrix grants routes the API lacks: {granted - served}"


def test_the_api_does_not_start_in_prod_without_a_jwt_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prod = Settings(_env_file=None, env=Environment.PROD)  # type: ignore[call-arg]
    monkeypatch.setattr(app_module, "get_settings", lambda: prod)

    with pytest.raises(RuntimeError, match="EMENTA_JWT_SECRET is required with EMENTA_ENV=prod"):
        build()

    secret = SecretStr("z" * 48)
    with_secret = Settings(_env_file=None, env=Environment.PROD, jwt_secret=secret)  # type: ignore[call-arg]
    monkeypatch.setattr(app_module, "get_settings", lambda: with_secret)
    assert isinstance(build(), FastAPI)


def test_openapi_offers_the_password_flow_on_protected_routes() -> None:
    document = build().openapi()

    scheme = document["components"]["securitySchemes"]["OAuth2PasswordBearer"]
    assert scheme["flows"]["password"]["tokenUrl"] == "/v1/auth/token"
    assert document["paths"]["/v1/legal-research"]["post"]["security"] == [
        {"OAuth2PasswordBearer": []}
    ]
    assert "security" not in document["paths"]["/v1/auth/token"]["post"]
