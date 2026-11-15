"""Long-term memory in the LangGraph store, on Postgres with pgvector.

A memory is one store item. Its value holds `text` (what was learned, the only field embedded),
`subjects` (the sorted pseudonymous ids of the people it mentions, so forgetting a person finds
it), `confidence` and `owner` (the namespace owner, whose permission the memory follows).
"""

import hashlib
import hmac
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from langgraph.store.base import BaseStore, PutOp, SearchItem
from langgraph.store.postgres import PostgresStore
from psycopg import Connection, sql
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from ementa_kit.memory.embeddings import LocalEmbeddings
from ementa_kit.rag import Embedder
from ementa_kit.telemetry import get_tracer

SCHEMA = "langgraph"  # shared with the LangGraph checkpointer, whose tables have other names
TEXT = "text"
SUBJECTS = "subjects"
SUBJECT_ID_PATTERN = r"^ds_[0-9a-f]{32}$"

_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")

tracer = get_tracer(__name__)


@dataclass(frozen=True)
class Namespace:
    """Where a memory lives and whose permission it follows: `kind` is the type of owner and
    `owner` its id, or None when the namespace belongs to the whole organization."""

    kind: str
    owner: str | None = None

    def __post_init__(self) -> None:
        for label in (self.kind, self.owner):
            if label is not None and (not label or "." in label):
                # The Postgres store joins the labels with dots
                raise ValueError(f"namespace labels are not empty and have no '.': {label!r}")

    @property
    def path(self) -> tuple[str, ...]:
        return (self.kind,) if self.owner is None else (self.kind, self.owner)


@dataclass(frozen=True)
class Memory:
    namespace: Namespace
    key: str
    text: str
    subjects: tuple[str, ...]
    confidence: float
    updated_at: datetime
    score: float | None = None  # cosine similarity to the query


def memory_value(
    namespace: Namespace, text: str, subjects: Sequence[str], confidence: float
) -> dict[str, Any]:
    """What a memory stores. `subjects` go sorted, so a filter on the list finds the same people."""
    return {
        TEXT: text,
        SUBJECTS: sorted(set(subjects)),
        "confidence": confidence,
        "owner": {"kind": namespace.kind, "id": namespace.owner},
    }


def subject_id(key: bytes, entity: str, value: str) -> str:
    """Pseudonymous id of the person behind `value`, an `entity` the PII guard found (`CPF`,
    `PESSOA`, `EMAIL`): an HMAC-SHA-256 of the entity and the value in the form the guard compares,
    so `123.456.789-09` and `12345678909` give one id. Without the key, the id does not lead back
    to the value, not even by trying every CPF."""
    from ementa_kit.guardrails.pii import canonical_value  # imports spaCy: only writing needs it

    message = f"{entity}:{canonical_value(entity, value)}".encode()
    return "ds_" + hmac.new(key, message, hashlib.sha256).hexdigest()[:32]


def open_postgres_store(
    database_url: str, embedder: Embedder, *, schema: str = SCHEMA, max_connections: int = 4
) -> PostgresStore:
    """The LangGraph store in `schema`, with a vector index on the text of each memory.

    Its tables (`store`, `store_vectors` and their migration tables) go to `schema` through the
    `search_path` of each connection; pgvector stays in `public`. The search is exact, with no
    HNSW index: an approximate scan filtered by namespace can return fewer items than asked, and
    a firm's memories are few enough to scan in milliseconds.

    Opens a connection pool for the life of the process (`store.conn.close()` ends it), creates
    the schema and runs the store migrations, which skip what is already there.
    """
    if not _IDENTIFIER.fullmatch(schema):
        raise ValueError(f"schema must be a lower-case SQL identifier, got {schema!r}")
    pool = cast(
        ConnectionPool[Connection[DictRow]],
        ConnectionPool(
            database_url,
            min_size=1,
            max_size=max_connections,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
                "options": f"-c search_path={schema},public",
            },
            open=True,
        ),
    )
    try:
        with pool.connection() as conn:
            conn.execute("create extension if not exists vector schema public")
            conn.execute(sql.SQL("create schema if not exists {}").format(sql.Identifier(schema)))
        store = PostgresStore(
            pool,
            index={
                "dims": embedder.dim,
                "embed": LocalEmbeddings(embedder),
                "fields": [TEXT],
                "ann_index_config": {"kind": "flat"},
            },
        )
        store.setup()
    except BaseException:
        pool.close()
        raise
    return store


def search_memories(
    store: BaseStore, namespaces: Sequence[Namespace], query: str, *, per_namespace: int = 3
) -> list[Memory]:
    """Up to `per_namespace` memories from each namespace, the closest to `query` first.

    The caller picks the namespaces the user may read: a case's memories only where the case
    itself may be seen.
    """
    with tracer.start_as_current_span(
        "retrieval memory",
        attributes={
            "gen_ai.operation.name": "retrieval",
            "gen_ai.data_source.id": "memory",
            "ementa.memory.namespaces": [n.kind for n in namespaces],
        },
    ) as span:
        found = [
            _memory(namespace, item)
            for namespace in namespaces
            for item in store.search(namespace.path, query=query, limit=per_namespace)
            if item.namespace == namespace.path and TEXT in item.value
        ]
        found.sort(key=lambda m: -(m.score or 0.0))
        span.set_attribute("ementa.memory.hits", len(found))
        return found


def forget_subject(store: BaseStore, subject: str) -> int:
    """Delete every memory that mentions the data subject `subject`, in every namespace (LGPD,
    art. 18). Returns how many went; the span records that count and nothing else.

    It reads the whole store, since the store filters on equality only, not on "the list holds".
    That is fine at the size of a firm's memories; past it, a GIN index on the subjects.
    """
    with tracer.start_as_current_span(
        "memory forget", attributes={"ementa.memory.operation": "forget"}
    ) as span:
        doomed = [
            PutOp(item.namespace, item.key, None)
            for item in _every_item(store)
            if _mentions(item.value, subject)
        ]
        if doomed:
            store.batch(doomed)
        span.set_attribute("ementa.memory.removed", len(doomed))
        return len(doomed)


def _memory(namespace: Namespace, item: SearchItem) -> Memory:
    value = item.value
    return Memory(
        namespace=namespace,
        key=item.key,
        text=str(value[TEXT]),
        subjects=tuple(value.get(SUBJECTS, ())),
        confidence=float(value.get("confidence", 1.0)),
        updated_at=item.updated_at,
        score=item.score,
    )


def _mentions(value: dict[str, Any], subject: str) -> bool:
    subjects = value.get(SUBJECTS)
    return isinstance(subjects, list) and subject in subjects


def _every_item(store: BaseStore, page: int = 1000) -> list[SearchItem]:
    """Every item, in one query. Pages by offset could skip some: the store orders them by
    `updated_at`, and the items saved in one batch share it."""
    limit = page
    while True:
        items = store.search((), limit=limit)
        if len(items) < limit:
            return items
        limit *= 2
