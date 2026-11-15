"""`DELETE /v1/data-subjects/{id}/memories`: LGPD forgetting, for the DPO and the sócia only.

Every name and CPF here is fictitious.
"""

import pytest
from fastapi.testclient import TestClient
from langgraph.store.memory import InMemoryStore

from ementa_api.app import create_app
from ementa_api.auth import Role, User, current_user
from ementa_kit.memory import Namespace, memory_value, subject_id

KEY = b"test-key"
MARIA = subject_id(KEY, "CPF", "123.456.789-09")
JOAO = subject_id(KEY, "CPF", "987.654.321-00")
CASE = Namespace("case", "c1")
LAWYER = Namespace("lawyer", "u1")


def store_with_memories() -> InMemoryStore:
    store = InMemoryStore()
    for key, namespace, text, subjects in [
        ("m1", CASE, "Maria Souza (123.456.789-09) quer acordo.", [MARIA]),
        ("m2", Namespace("case", "c2"), "Maria Souza é testemunha do João Pereira.", [MARIA, JOAO]),
        ("m3", CASE, "João Pereira mora em Canoas.", [JOAO]),
        ("m4", LAWYER, "Gosta de respostas em tópicos.", []),
    ]:
        store.put(namespace.path, key, memory_value(namespace, text, subjects, 0.9))
    return store


def client(store: InMemoryStore, role: Role) -> TestClient:
    app = create_app(memory=lambda: store)
    app.dependency_overrides[current_user] = lambda: User(id="u9", name="Fictícia", role=role)
    return TestClient(app, raise_server_exceptions=False)


def left(store: InMemoryStore) -> list[str]:
    return sorted(item.key for item in store.search((), limit=100))


@pytest.mark.parametrize("role", [Role.DPO, Role.SOCIA])
def test_the_dpo_or_the_socia_forgets_a_data_subject(role: Role) -> None:
    store = store_with_memories()
    api = client(store, role)

    response = api.delete(f"/v1/data-subjects/{MARIA}/memories")

    assert response.status_code == 200
    assert response.json() == {"removed": 2}  # how many, and nothing about the person
    assert left(store) == ["m3", "m4"]
    assert api.delete(f"/v1/data-subjects/{MARIA}/memories").json() == {"removed": 0}


@pytest.mark.parametrize("role", [Role.ASSOCIADO, Role.ESTAGIARIO, Role.CONTROLADORIA, Role.TI])
def test_other_roles_may_not_forget(role: Role) -> None:
    store = store_with_memories()

    response = client(store, role).delete(f"/v1/data-subjects/{MARIA}/memories")

    assert response.status_code == 403
    assert response.headers["content-type"] == "application/problem+json"
    assert left(store) == ["m1", "m2", "m3", "m4"]


@pytest.mark.parametrize("raw", ["123.456.789-09", "12345678909", "Maria Souza", "ds_123"])
def test_the_path_takes_the_pseudonymous_id_never_the_identifier(raw: str) -> None:
    store = store_with_memories()

    response = client(store, Role.DPO).delete(f"/v1/data-subjects/{raw}/memories")

    assert response.status_code == 422
    body = response.json()
    assert body["type"] == "/problems/validation-error"
    assert body["instance"] == "/v1/data-subjects/{subject_id}/memories"  # not the path sent
    assert raw not in response.text
    assert left(store) == ["m1", "m2", "m3", "m4"]
