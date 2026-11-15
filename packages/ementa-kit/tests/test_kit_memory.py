"""Long-term memory: the store, the writer with a fake `fast` model, search and forgetting.

Every name and CPF here is fictitious. The Postgres tests need EMENTA_TEST_DATABASE_URL.
"""

import hashlib
import json
import math
import os
import re
import uuid
from collections.abc import Iterator, Sequence
from typing import Any

import psycopg
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatResult
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from langgraph.store.postgres import PostgresStore
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from psycopg import sql
from pydantic import Field

from ementa_kit.config import Settings
from ementa_kit.context import assert_append_only
from ementa_kit.guardrails.pii import PiiGuard, build_pii_guard
from ementa_kit.llm import ModelRegistry, ModelRole, ModelSpec
from ementa_kit.memory import (
    MAX_MEMORIES,
    SUBJECT_ID_PATTERN,
    LocalEmbeddings,
    MemoryWriter,
    Namespace,
    forget_subject,
    memory_value,
    open_postgres_store,
    search_memories,
    subject_id,
)
from ementa_kit.memory import store as store_module
from ementa_kit.memory import writer as writer_module
from ementa_kit.prompts import Prompt

KEY = b"test-key"
PROMPT = Prompt("memory.extract", 1, "v1 teste", "Extraia memórias em JSON.")
LAWYER = Namespace("lawyer", "u1")
OTHER_LAWYER = Namespace("lawyer", "u2")
CASE = Namespace("case", "c1")
OTHER_CASE = Namespace("case", "c2")
FIRM = Namespace("firm")

HUMAN = "Minha cliente Maria Souza, CPF 123.456.789-09, quer tentar acordo antes de ajuizar."
TURN = [HumanMessage(HUMAN), AIMessage("Entendido, vou preparar uma proposta de acordo.")]
MARIA = subject_id(KEY, "PESSOA", "Maria Souza")
JOAO = subject_id(KEY, "PESSOA", "João Pereira")


class HashEmbedder:
    """Bag of words hashed into 256 dimensions: texts with the same words get the same vector.
    Records the side of the embedder each text went through."""

    model_name = "hash"
    dim = 256

    def __init__(self) -> None:
        self.sides: list[str] = []

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        self.sides += ["query"] * len(texts)
        return [_bag(t) for t in texts]

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        self.sides += ["passage"] * len(texts)
        return [_bag(t) for t in texts]


def _bag(text: str) -> list[float]:
    vector = [0.0] * HashEmbedder.dim
    for word in re.findall(r"\w+", text.lower()):
        vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % HashEmbedder.dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


class ScriptedChat(GenericFakeChatModel):
    """Replies in order and records a copy of each request."""

    calls: list[list[BaseMessage]] = Field(default_factory=list)

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        self.calls.append([m.model_copy(deep=True) for m in messages])
        return super()._generate(messages, *args, **kwargs)


def reply(*memories: dict[str, Any]) -> AIMessage:
    return AIMessage(json.dumps({"memories": list(memories)}, ensure_ascii=False))


def remember(scope: str, text: str, confidence: float = 0.9) -> dict[str, Any]:
    return {"scope": scope, "text": text, "confidence": confidence}


@pytest.fixture(scope="module")
def guard() -> PiiGuard:
    return build_pii_guard()


def new_store() -> InMemoryStore:
    embeddings = LocalEmbeddings(HashEmbedder())
    return InMemoryStore(index={"dims": HashEmbedder.dim, "embed": embeddings, "fields": ["text"]})


def make_writer(
    guard: PiiGuard,
    replies: list[AIMessage],
    store: InMemoryStore | None = None,
    similarity: float = 0.85,
) -> tuple[MemoryWriter, ScriptedChat, list[ModelRole]]:
    chat = ScriptedChat(messages=iter(replies))
    roles: list[ModelRole] = []

    def factory(spec: ModelSpec) -> ScriptedChat:
        roles.append(spec.role)
        return chat

    registry = ModelRegistry(Settings(_env_file=None), factory=factory)  # type: ignore[call-arg]
    writer = MemoryWriter(
        store if store is not None else new_store(),
        registry,
        lambda: guard,
        PROMPT,
        subject_key=KEY,
        similarity=similarity,
        min_confidence=0.7,
    )
    return writer, chat, roles


def markers(guard: PiiGuard, text: str) -> dict[str, str]:
    """Value to marker, as the guard masks `text` at the start of a conversation."""
    return {value: marker for marker, value in guard.pseudonymize(text).mapping.items()}


def texts(store: BaseStore, namespace: Namespace) -> list[str]:
    return sorted(item.value["text"] for item in store.search(namespace.path, limit=100))


def put(store: BaseStore, namespace: Namespace, text: str, subjects: Sequence[str] = ()) -> None:
    store.put(namespace.path, uuid.uuid4().hex, memory_value(namespace, text, subjects, 0.9))


# Writer ------------------------------------------------------------------------------------


def test_the_model_sees_markers_and_the_store_keeps_values_tagged_by_subject(
    guard: PiiGuard,
) -> None:
    m = markers(guard, HUMAN)
    person, cpf = m["Maria Souza"], m["123.456.789-09"]
    writer, chat, roles = make_writer(
        guard,
        [
            reply(
                remember("case", f"{person} ({cpf}) quer tentar acordo antes de ajuizar."),
                remember("lawyer", "Gosta de respostas em tópicos.", 0.8),
            )
        ],
    )

    written = writer.write(TURN, [LAWYER, CASE])

    assert roles == [ModelRole.FAST]
    [request] = chat.calls
    sent = "\n".join(message.text for message in request)
    assert "Maria Souza" not in sent and "123.456.789-09" not in sent
    assert f"Minha cliente {person}, CPF {cpf}" in sent
    assert "<escopos>\nlawyer, case\n</escopos>" in sent
    assert [(w.namespace, w.updated) for w in written] == [(CASE, False), (LAWYER, False)]
    [card] = writer.store.search(CASE.path)
    assert card.value == {
        "text": "Maria Souza (123.456.789-09) quer tentar acordo antes de ajuizar.",
        "subjects": sorted([MARIA, subject_id(KEY, "CPF", "12345678909")]),
        "confidence": 0.9,
        "owner": {"kind": "case", "id": "c1"},
    }
    [preference] = writer.store.search(LAWYER.path)
    assert preference.value["subjects"] == []
    assert preference.value["owner"] == {"kind": "lawyer", "id": "u1"}


def test_a_candidate_off_target_unsure_invalid_or_with_a_made_up_marker_is_dropped(
    guard: PiiGuard,
) -> None:
    writer, _, _ = make_writer(
        guard,
        [
            reply(
                remember("firm", "Tese aprovada: acordo sempre."),  # not a target of this turn
                remember("case", "Talvez a cliente queira acordo.", 0.4),  # under 0.7
                remember("case", "[PESSOA_9] é testemunha."),  # a marker of no one
                remember("case", ""),  # not a memory
                {"scope": "case", "text": "Sem confiança."},
                remember("case", "A cliente quer acordo."),
            )
        ],
    )

    written = writer.write(TURN, [LAWYER, CASE])

    assert [w.namespace for w in written] == [CASE]
    assert texts(writer.store, CASE) == ["A cliente quer acordo."]
    assert writer.store.search(FIRM.path) == []


def test_at_most_five_memories_come_from_one_turn(guard: PiiGuard) -> None:
    facts = ["Petições curtas.", "Cita súmulas do STJ.", "Evita latim.", "Usa tópicos."]
    facts += ["Revisa prazos duas vezes.", "Pede resumo no início.", "Detesta notas de rodapé."]
    writer, _, _ = make_writer(guard, [reply(*(remember("lawyer", f) for f in facts))])

    assert len(writer.write(TURN, [LAWYER])) == MAX_MEMORIES == 5
    assert texts(writer.store, LAWYER) == sorted(facts[:5])


def test_a_near_copy_about_the_same_people_replaces_the_stored_memory(guard: PiiGuard) -> None:
    store = new_store()
    first, _, _ = make_writer(
        guard, [reply(remember("lawyer", "Gosta de respostas em tópicos."))], store
    )
    [old] = first.write(TURN, [LAWYER])
    second, _, _ = make_writer(
        guard,
        [
            reply(
                remember("lawyer", "Gosta de respostas em tópicos curtos."),  # cosine 0.91
                remember("lawyer", "Cita súmulas do STJ."),  # another fact
            )
        ],
        store,
    )

    new, other = second.write(TURN, [LAWYER])

    assert (new.key, new.updated) == (old.key, True)
    assert not other.updated
    assert texts(store, LAWYER) == ["Cita súmulas do STJ.", "Gosta de respostas em tópicos curtos."]


def test_the_same_sentence_about_another_person_is_a_new_memory(guard: PiiGuard) -> None:
    """Even with a threshold any rewrite passes, memories about different people never merge."""
    store = new_store()
    turns = [
        ("Maria Souza", "A cliente Maria Souza quer acordo."),
        ("João Pereira", "O cliente João Pereira quer acordo."),
        ("Maria Souza", "A cliente Maria Souza quer muito um acordo."),
    ]
    written = []
    for name, text in turns:
        person = markers(guard, text)[name]
        writer, _, _ = make_writer(
            guard, [reply(remember("case", f"{person} quer acordo."))], store, similarity=0.3
        )
        [memory] = writer.write([HumanMessage(text)], [CASE])
        written.append(memory)

    assert [w.updated for w in written] == [False, False, True]
    assert written[2].key == written[0].key
    by_subject = {tuple(i.value["subjects"]): i.value["text"] for i in store.search(CASE.path)}
    assert by_subject == {
        (MARIA,): "Maria Souza quer acordo.",
        (JOAO,): "João Pereira quer acordo.",
    }


def test_the_conversation_markers_are_kept(guard: PiiGuard) -> None:
    writer, chat, _ = make_writer(guard, [reply(remember("case", "[PESSOA_2] quer acordo."))])

    writer.write(
        [HumanMessage("A cliente Maria Souza quer acordo.")],
        [CASE],
        mapping={"[PESSOA_1]": "João Pereira"},
    )

    assert "[PESSOA_2] quer acordo" in chat.calls[0][-1].text
    assert texts(writer.store, CASE) == ["Maria Souza quer acordo."]


def test_an_invalid_reply_is_asked_again_once_appending_the_correction(guard: PiiGuard) -> None:
    writer, chat, _ = make_writer(
        guard, [AIMessage("Claro! Seguem as memórias."), reply(remember("lawyer", "Evita latim."))]
    )

    assert len(writer.write(TURN, [LAWYER])) == 1
    first, second = chat.calls
    assert_append_only(chat.calls)  # the bad reply and the correction go after the request
    assert len(second) == len(first) + 2
    assert "JSON" in second[-1].text


def test_a_second_invalid_reply_writes_nothing(guard: PiiGuard) -> None:
    writer, chat, _ = make_writer(guard, [AIMessage("não sei"), AIMessage('{"memorias": []}')])

    assert writer.write(TURN, [LAWYER]) == []
    assert len(chat.calls) == 2
    assert writer.store.search(LAWYER.path) == []


def test_a_reply_in_a_code_fence_is_read(guard: PiiGuard) -> None:
    memories = '{"memories": [{"scope": "lawyer", "text": "Evita latim.", "confidence": 1}]}'
    fenced = f"```json\n{memories}\n```"
    writer, _, _ = make_writer(guard, [AIMessage(fenced)])

    assert len(writer.write(TURN, [LAWYER])) == 1


def test_no_target_or_no_conversation_text_calls_no_model(guard: PiiGuard) -> None:
    writer, chat, roles = make_writer(guard, [])
    only_system_and_tools = [SystemMessage("regras"), ToolMessage("resultado", tool_call_id="1")]

    assert writer.write(TURN, []) == []
    assert writer.write(only_system_and_tools, [LAWYER]) == []
    assert chat.calls == [] and roles == []


def test_the_write_span_counts_and_records_no_text(
    guard: PiiGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(writer_module, "tracer", provider.get_tracer("test"))
    writer, _, _ = make_writer(
        guard, [reply(remember("lawyer", "Evita latim."), remember("lawyer", "Evita latim!"))]
    )

    writer.write(TURN, [LAWYER])

    [span] = exporter.get_finished_spans()
    assert span.name == "memory write"
    assert dict(span.attributes or {}) == {
        "ementa.memory.operation": "write",
        "ementa.prompt.id": "memory.extract",
        "ementa.prompt.version": 1,
        "ementa.memory.candidates": 2,
        "ementa.memory.inserted": 1,
        "ementa.memory.updated": 1,
    }


# Store, search and forgetting ---------------------------------------------------------------


def test_saved_and_searched_texts_go_through_the_query_side_of_the_embedder() -> None:
    embedder = HashEmbedder()
    embeddings = LocalEmbeddings(embedder)

    assert embeddings.embed_documents(["acordo antes"]) == [embeddings.embed_query("acordo antes")]
    assert embedder.sides == ["query", "query"]


def test_namespaces_map_to_store_paths_without_dots() -> None:
    assert (LAWYER.path, FIRM.path) == (("lawyer", "u1"), ("firm",))
    with pytest.raises(ValueError, match=r"no '\.'"):
        Namespace("case", "0001234-56.2024.8.21.0001")  # a CNJ number is not a case id
    with pytest.raises(ValueError):
        Namespace("")


def test_subject_ids_are_keyed_and_ignore_how_the_value_is_written() -> None:
    cpf = subject_id(KEY, "CPF", "123.456.789-09")

    assert re.fullmatch(SUBJECT_ID_PATTERN, cpf)
    assert cpf == subject_id(KEY, "CPF", "12345678909")
    assert cpf != subject_id(b"another key", "CPF", "123.456.789-09")
    assert cpf != subject_id(KEY, "CNPJ", "123.456.789-09")
    assert subject_id(KEY, "PESSOA", "José Souza") == subject_id(KEY, "PESSOA", "jose  souza")
    assert memory_value(LAWYER, "x", [JOAO, MARIA, JOAO], 1.0)["subjects"] == sorted([JOAO, MARIA])


def test_search_brings_the_closest_memories_of_each_namespace_asked() -> None:
    store = new_store()
    put(store, LAWYER, "Gosta de respostas em tópicos.")
    put(store, LAWYER, "Cita súmulas do STJ.")
    put(store, OTHER_LAWYER, "Gosta de respostas em tópicos.")
    put(store, CASE, "Audiência de conciliação em 10/12/2026.")

    found = search_memories(store, [LAWYER, CASE], "respostas em tópicos", per_namespace=1)

    assert [(m.namespace, m.text) for m in found] == [
        (LAWYER, "Gosta de respostas em tópicos."),
        (CASE, "Audiência de conciliação em 10/12/2026."),
    ]
    assert (found[0].score or 0) > (found[1].score or 0)


def test_forgetting_deletes_every_memory_of_the_subject_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(store_module, "tracer", provider.get_tracer("test"))
    store = new_store()
    put(store, CASE, "Maria Souza quer acordo.", [MARIA])
    put(store, OTHER_CASE, "Maria Souza e João Pereira são vizinhos.", [MARIA, JOAO])
    put(store, CASE, "João Pereira é testemunha.", [JOAO])
    put(store, LAWYER, "Gosta de respostas em tópicos.")
    store.put(("other",), "x", {"note": "não é memória"})

    assert forget_subject(store, MARIA) == 2
    assert forget_subject(store, MARIA) == 0

    left = sorted(str(item.value.get("text")) for item in store.search((), limit=100))
    assert left == ["Gosta de respostas em tópicos.", "João Pereira é testemunha.", "None"]
    found = search_memories(store, [CASE, OTHER_CASE], "Maria Souza quer acordo")
    assert all(MARIA not in m.subjects for m in found)
    spans = [s for s in exporter.get_finished_spans() if s.name == "memory forget"]
    assert [dict(s.attributes or {}) for s in spans] == [
        {"ementa.memory.operation": "forget", "ementa.memory.removed": n} for n in (2, 0)
    ]


def test_every_item_is_read_in_one_query_whatever_the_page() -> None:
    store = new_store()
    for n in range(5):
        put(store, CASE, f"Fato {n}.", [MARIA])

    assert len(store_module._every_item(store, page=2)) == 5
    assert forget_subject(store, MARIA) == 5


# Postgres -----------------------------------------------------------------------------------

DATABASE_URL = os.environ.get("EMENTA_TEST_DATABASE_URL")


@pytest.fixture
def pg() -> Iterator[tuple[PostgresStore, str]]:
    if not DATABASE_URL:
        pytest.skip("EMENTA_TEST_DATABASE_URL not set")
    schema = f"test_memory_{uuid.uuid4().hex[:8]}"
    store = open_postgres_store(DATABASE_URL, HashEmbedder(), schema=schema)
    yield store, schema
    store.conn.close()
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(schema)))


def tables(schema: str) -> set[str]:
    assert DATABASE_URL
    with psycopg.connect(DATABASE_URL) as conn:
        rows = conn.execute(
            "select table_name from information_schema.tables where table_schema = %s", (schema,)
        ).fetchall()
    return {row[0] for row in rows}


def vectors(schema: str) -> int:
    assert DATABASE_URL
    with psycopg.connect(DATABASE_URL) as conn:
        query = sql.SQL("select count(*) from {}.store_vectors").format(sql.Identifier(schema))
        row = conn.execute(query).fetchone()
    return int(row[0]) if row else 0


@pytest.mark.integration
def test_the_store_tables_live_in_their_own_schema(pg: tuple[PostgresStore, str]) -> None:
    _, schema = pg

    assert tables(schema) == {"store", "store_vectors", "store_migrations", "vector_migrations"}
    assert not tables("public") & {"store", "store_vectors"}


@pytest.mark.integration
def test_postgres_finds_memories_by_meaning_and_forgets_them_by_subject(
    pg: tuple[PostgresStore, str],
) -> None:
    store, schema = pg
    put(store, CASE, "Maria Souza quer acordo antes de ajuizar.", [MARIA])
    put(store, OTHER_CASE, "Maria Souza mora em Porto Alegre.", [MARIA])
    put(store, CASE, "João Pereira quer acordo antes de ajuizar.", [JOAO])

    [best, other] = search_memories(store, [CASE], "Maria Souza quer acordo antes de ajuizar.")
    assert best.text == "Maria Souza quer acordo antes de ajuizar."
    assert best.score is not None and best.score > 0.99 > (other.score or 0)
    same_people = store.search(CASE.path, query="quer acordo", filter={"subjects": [MARIA]})
    assert [i.value["text"] for i in same_people] == ["Maria Souza quer acordo antes de ajuizar."]
    assert vectors(schema) == 3

    assert forget_subject(store, MARIA) == 2

    assert [m.text for m in search_memories(store, [CASE, OTHER_CASE], "Maria Souza")] == [
        "João Pereira quer acordo antes de ajuizar."
    ]
    assert vectors(schema) == 1  # the embeddings went with the memories


@pytest.mark.integration
def test_reopening_the_store_keeps_the_memories(pg: tuple[PostgresStore, str]) -> None:
    store, schema = pg
    put(store, LAWYER, "Gosta de respostas em tópicos.")
    assert DATABASE_URL

    again = open_postgres_store(DATABASE_URL, HashEmbedder(), schema=schema)  # migrations rerun
    try:
        assert texts(again, LAWYER) == ["Gosta de respostas em tópicos."]
    finally:
        again.conn.close()


def test_the_schema_must_be_a_plain_identifier() -> None:
    with pytest.raises(ValueError, match="identifier"):
        open_postgres_store("postgresql://x", HashEmbedder(), schema="langgraph; drop table x")
