import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ementa.agents.legal_research import Citation, ResearchAnswer
from ementa.domain.citations import RemovedCitation
from ementa.users import FirmUser
from ementa_api.app import create_app
from ementa_api.auth import Role, User, create_access_token
from ementa_kit.llm import ModelUnavailableError

OPENAPI = Path(__file__).parents[1] / "openapi.json"
QUESTION = "Comprei pela internet e me arrependi. Tenho quanto tempo para desistir?"


class FakeResearcher:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.questions: list[str] = []

    def ask(self, question: str) -> ResearchAnswer:
        self.questions.append(question)
        if self.error:
            raise self.error
        return ResearchAnswer(
            question=question,
            answer="Você tem 7 dias para desistir [cdc:art49].",
            citations=[
                Citation("cdc:art49", "Código de Defesa do Consumidor, art. 49", "https://x")
            ],
            removed=[RemovedCitation("stj:sumula:999", "Frase.", "not in the sources")],
            sources=["cdc:art49"],
            queries=[question],
            attempts=1,
            prompt="legal_research.answer@1",
            model="fake-model",
        )


class NoUsers:
    def authenticate(self, username: str, password: str) -> FirmUser | None:
        return None


def auth(role: Role = Role.ASSOCIADO) -> dict[str, str]:
    token = create_access_token(User(id="u-1", name="Pessoa de Teste", role=role))
    return {"Authorization": f"Bearer {token}"}


def client(researcher: FakeResearcher | None = None, chunks: int | Exception = 118) -> TestClient:
    def count() -> int:
        if isinstance(chunks, Exception):
            raise chunks
        return chunks

    fake = researcher or FakeResearcher()
    app = create_app(researcher=lambda: fake, chunks=count, users=NoUsers)
    return TestClient(app, raise_server_exceptions=False, headers=auth())


def test_legal_research_returns_the_verified_answer() -> None:
    fake = FakeResearcher()
    response = client(fake).post("/v1/legal-research", json={"question": QUESTION})

    assert response.status_code == 200
    body = response.json()
    assert body["citations"][0]["source_id"] == "cdc:art49"
    assert body["removed"] == [{"reference": "stj:sumula:999", "reason": "not in the sources"}]
    assert body["grounded"] is True
    assert body["data_class"] == "public"
    assert fake.questions == [QUESTION]


CPF = "123.456.789-09"


@pytest.mark.parametrize(
    "payload",
    [{"question": CPF[:4]}, {"question": QUESTION, "cpf": CPF}, {}],
)
def test_invalid_request_is_a_problem_without_the_values(payload: dict[str, object]) -> None:
    response = client().post("/v1/legal-research", json=payload)

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["type"] == "/problems/validation-error"
    assert body["instance"] == "/v1/legal-research"
    assert body["errors"]
    assert all(set(error) == {"loc", "msg", "type"} for error in body["errors"])
    assert CPF[:4] not in response.text
    assert QUESTION not in response.text


def test_missing_model_key_is_503() -> None:
    fake = FakeResearcher(ModelUnavailableError("OPENAI_API_KEY is not set"))
    response = client(fake).post("/v1/legal-research", json={"question": QUESTION})

    assert response.status_code == 503
    assert response.json()["type"] == "/problems/model-unavailable"


def test_unexpected_error_is_500_without_details() -> None:
    fake = FakeResearcher(RuntimeError("secret internals"))
    response = client(fake).post("/v1/legal-research", json={"question": QUESTION})

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    assert "secret" not in response.text


def test_unknown_route_is_a_problem() -> None:
    response = client().get("/v1/nada")

    assert response.status_code == 404
    assert response.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "instance": "/v1/nada",
    }


def test_health_and_readiness() -> None:
    assert client().get("/healthz").json() == {"status": "ok"}
    assert client().get("/readyz").json() == {"status": "ready", "chunks": 118}

    empty = client(chunks=0).get("/readyz")
    down = client(chunks=OSError("refused")).get("/readyz")
    assert empty.status_code == down.status_code == 503
    assert "ingest" in empty.json()["detail"]
    assert down.json()["type"] == "/problems/not-ready"


def test_openapi_contract_is_up_to_date() -> None:
    """The committed contract changes only on purpose: regenerate it with
    `uv run ementa-api --openapi > apps/api/openapi.json` and review the diff."""
    current = client().app.openapi()  # type: ignore[attr-defined]
    assert json.loads(OPENAPI.read_text(encoding="utf-8")) == current


class EchoResearcher(FakeResearcher):
    """Answers with whatever markers it received, as a cloud model would."""

    def ask(self, question: str) -> ResearchAnswer:
        answer = super().ask(question)
        markers = " ".join(w.strip(".,?") for w in question.split() if w.startswith("["))
        return replace(answer, answer=f"Recebi o pedido de {markers}.")


def test_cpf_reaches_the_model_as_a_marker_and_comes_back_rehydrated() -> None:
    from ementa.pii import PrivateResearcher

    model = EchoResearcher()
    app = create_app(researcher=lambda: PrivateResearcher(model), chunks=lambda: 1, users=NoUsers)
    question = "Meu CPF é 123.456.789-09 e fui negativado sem aviso. Posso pedir indenização?"

    response = TestClient(app, headers=auth()).post(
        "/v1/legal-research", json={"question": question}
    )

    assert response.status_code == 200
    assert "[CPF_1]" in model.questions[0]
    assert "123.456.789-09" not in model.questions[0]
    assert response.json()["answer"] == "Recebi o pedido de 123.456.789-09."
