"""The firm's memory: namespaces, what the assistant reads, data subject ids and forgetting.

Every name and CPF here is fictitious. The Postgres test needs EMENTA_TEST_DATABASE_URL.
"""

import hashlib
import json
import math
import os
import re
import uuid
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatResult
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from psycopg import sql
from pydantic import Field

from ementa import services
from ementa.memory import (
    EXTRACT_PROMPT,
    FIRM,
    DataSubject,
    case,
    chat_targets,
    data_subject_ids,
    lawyer,
    memory_section,
    relevant_memories,
    subject_key,
)
from ementa.pii import pii_guard
from ementa.prompts import get_prompt
from ementa_kit.config import Settings
from ementa_kit.context import Section, approx_tokens
from ementa_kit.llm import ModelRegistry, ModelSpec
from ementa_kit.memory import (
    LocalEmbeddings,
    Memory,
    MemoryWriter,
    Namespace,
    forget_subject,
    memory_value,
    open_postgres_store,
    subject_id,
)

DATABASE_URL = os.environ.get("EMENTA_TEST_DATABASE_URL")
HUMAN = (
    "A cliente Maria Souza, CPF 123.456.789-09, quer tentar acordo antes de ajuizar. "
    "Me responda sempre em tópicos."
)
TURN = [HumanMessage(HUMAN), AIMessage("Entendido. Vou preparar a proposta de acordo.")]


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


class HashEmbedder:
    """Bag of words hashed into 256 dimensions: texts with the same words get the same vector."""

    model_name = "hash"
    dim = 256

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._bag(t) for t in texts]

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._bag(t) for t in texts]

    def _bag(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for word in re.findall(r"\w+", text.lower()):
            vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]


class ScriptedChat(GenericFakeChatModel):
    calls: list[list[BaseMessage]] = Field(default_factory=list)

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        self.calls.append([m.model_copy(deep=True) for m in messages])
        return super()._generate(messages, *args, **kwargs)


def new_store() -> InMemoryStore:
    embeddings = LocalEmbeddings(HashEmbedder())
    return InMemoryStore(index={"dims": HashEmbedder.dim, "embed": embeddings, "fields": ["text"]})


def put(store: BaseStore, namespace: Namespace, text: str) -> None:
    store.put(namespace.path, uuid.uuid4().hex, memory_value(namespace, text, [], 0.9))


def memory(namespace: Namespace, text: str, score: float) -> Memory:
    when = datetime(2026, 11, 15, tzinfo=UTC)
    return Memory(namespace, uuid.uuid4().hex, text, (), 0.9, when, score)


# Namespaces and reading ---------------------------------------------------------------------


def test_namespaces_follow_the_architecture() -> None:
    assert (lawyer("u1").path, case("c1").path, FIRM.path) == (
        ("lawyer", "u1"),
        ("case", "c1"),
        ("firm",),
    )
    assert chat_targets("u1") == [lawyer("u1")]
    assert chat_targets("u1", "c1") == [lawyer("u1"), case("c1")]  # a chat never writes a thesis


def test_the_assistant_reads_the_user_the_case_and_the_firm_only() -> None:
    store = new_store()
    for namespace in (lawyer("u1"), lawyer("u2"), case("c1"), case("c2"), FIRM):
        put(store, namespace, f"Acordo em {namespace.path[-1]}.")

    found = relevant_memories(store, "acordo", user_id="u1", case_id="c1")

    assert {m.namespace for m in found} == {lawyer("u1"), case("c1"), FIRM}
    no_case = relevant_memories(store, "acordo", user_id="u1")
    assert {m.namespace for m in no_case} == {lawyer("u1"), FIRM}


def test_the_memory_section_groups_by_namespace_and_keeps_the_best_within_its_budget() -> None:
    memories = [
        memory(case("c1"), "A cliente quer acordo.", 0.9),
        memory(lawyer("u1"), "Gosta de respostas em tópicos.", 0.8),
        memory(FIRM, "Cobrança indevida se devolve em dobro [cdc:art42:pu].", 0.7),
        memory(case("c1"), "Audiência em 10/12/2026.", 0.6),
        memory(Namespace("other"), "Fora dos namespaces do escritório.", 1.0),
    ]

    section = memory_section(memories)

    assert section is not None and section.name == "memorias"
    assert section.content == (
        "Preferências do advogado:\n- Gosta de respostas em tópicos.\n"
        "Ficha do processo:\n- A cliente quer acordo.\n- Audiência em 10/12/2026.\n"
        "Teses do escritório:\n- Cobrança indevida se devolve em dobro [cdc:art42:pu]."
    )
    best = "Ficha do processo:\n- A cliente quer acordo."
    assert memory_section(memories, max_tokens=approx_tokens(best)) == Section("memorias", best)
    assert memory_section([]) is None


# Data subjects ------------------------------------------------------------------------------


def test_the_data_subject_key_is_required_in_prod() -> None:
    assert subject_key(settings(data_subject_key="segredo")) == b"segredo"
    assert subject_key(settings(env="ci")) == subject_key(settings())  # the development key
    with pytest.raises(RuntimeError, match="EMENTA_DATA_SUBJECT_KEY"):
        subject_key(settings(env="prod"))


def test_the_dpo_gets_the_ids_of_the_people_named_without_their_values() -> None:
    config = settings()
    key = subject_key(config)

    found = data_subject_ids("Maria Souza, CPF 123.456.789-09", config)

    assert found == [
        DataSubject("PESSOA", subject_id(key, "PESSOA", "Maria Souza")),
        DataSubject("CPF", subject_id(key, "CPF", "12345678909")),
    ]
    assert data_subject_ids("CPF 12345678909", config) == found[1:]  # the mask does not matter
    assert all("123" not in s.id and "Maria" not in s.id for s in found)


# Forgetting, end to end ---------------------------------------------------------------------


def writer_for(store: BaseStore, config: Settings) -> MemoryWriter:
    """The writer as `services.memory_writer()` builds it, with a scripted `fast` model that
    keeps the case fact and the preference of `TURN`, using the markers it was shown (the user
    speaks first, so the turn's markers are those of `HUMAN`)."""
    marker = {value: m for m, value in pii_guard().pseudonymize(HUMAN).mapping.items()}
    person, cpf = marker["Maria Souza"], marker["123.456.789-09"]
    memories = [
        {"scope": "case", "text": f"{person} ({cpf}) quer tentar acordo.", "confidence": 0.9},
        {"scope": "lawyer", "text": "Gosta de respostas em tópicos.", "confidence": 0.8},
    ]
    chat = ScriptedChat(messages=iter([AIMessage(json.dumps({"memories": memories}))]))

    def factory(spec: ModelSpec) -> ScriptedChat:
        return chat

    return MemoryWriter(
        store,
        ModelRegistry(config, factory=factory),
        pii_guard,
        get_prompt(*EXTRACT_PROMPT),
        subject_key=subject_key(config),
        similarity=config.memory_dedupe_similarity,
        min_confidence=config.memory_min_confidence,
    )


def forget_and_search(store: BaseStore) -> None:
    config = settings()
    writer = writer_for(store, config)

    written = writer.write(TURN, chat_targets("u1", "c1"))

    assert len(written) == 2
    before = relevant_memories(store, "a cliente quer acordo?", user_id="u1", case_id="c1")
    assert "Maria Souza (123.456.789-09) quer tentar acordo." in [m.text for m in before]

    [cpf] = data_subject_ids("CPF 123.456.789-09", config)
    assert forget_subject(store, cpf.id) == 1
    assert forget_subject(store, cpf.id) == 0  # nothing left to forget

    after = relevant_memories(store, "a cliente quer acordo?", user_id="u1", case_id="c1")
    assert all("Maria Souza" not in m.text for m in after)
    assert [m.text for m in after] == ["Gosta de respostas em tópicos."]


def test_a_forgotten_subject_no_longer_comes_back_from_the_memory_search() -> None:
    forget_and_search(new_store())


@pytest.fixture
def pg_store() -> Iterator[BaseStore]:
    if not DATABASE_URL:
        pytest.skip("EMENTA_TEST_DATABASE_URL not set")
    schema = f"test_memory_{uuid.uuid4().hex[:8]}"
    store = open_postgres_store(DATABASE_URL, HashEmbedder(), schema=schema)
    yield store
    store.conn.close()
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(schema)))


@pytest.mark.integration
def test_a_forgotten_subject_no_longer_comes_back_from_the_postgres_memory(
    pg_store: BaseStore,
) -> None:
    forget_and_search(pg_store)


# Services -----------------------------------------------------------------------------------


@pytest.fixture
def writer_service(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., MemoryWriter]]:
    def build(**overrides: Any) -> MemoryWriter:
        config = settings(**overrides)
        monkeypatch.setattr(services, "get_settings", lambda: config)
        monkeypatch.setattr(services, "memory_store", lambda: InMemoryStore())
        monkeypatch.setattr(services, "model_registry", lambda: ModelRegistry(config))
        return services.memory_writer()

    services.memory_writer.cache_clear()
    yield build
    services.memory_writer.cache_clear()


def test_the_memory_writer_service_follows_the_settings(
    writer_service: Callable[..., MemoryWriter],
) -> None:
    writer = writer_service(
        memory_dedupe_similarity=0.95, memory_min_confidence=0.8, data_subject_key="k"
    )

    assert (writer.similarity, writer.min_confidence, writer.subject_key) == (0.95, 0.8, b"k")
    assert (writer.prompt.id, writer.prompt.version) == EXTRACT_PROMPT
    assert writer.guard is pii_guard
