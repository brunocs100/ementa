"""The Postgres checkpointer on a real database. Set EMENTA_TEST_DATABASE_URL to run."""

import operator
import os
import uuid
from collections.abc import Iterator
from typing import Annotated, TypedDict

import psycopg
import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import START, StateGraph
from psycopg import sql

from ementa_kit.memory import postgres_checkpointer

DATABASE_URL = os.environ.get("EMENTA_TEST_DATABASE_URL", "")


class Counter(TypedDict):
    seen: Annotated[list[str], operator.add]


def counter_graph(saver: PostgresSaver):  # type: ignore[no-untyped-def]  # LangGraph's generics
    graph = StateGraph(Counter)
    graph.add_node("echo", lambda state: {})
    graph.add_edge(START, "echo")
    return graph.compile(checkpointer=saver)


@pytest.fixture
def schema() -> Iterator[str]:
    if not DATABASE_URL:
        pytest.skip("EMENTA_TEST_DATABASE_URL not set")
    name = f"test_{uuid.uuid4().hex[:8]}"
    yield name
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(name)))


@pytest.mark.integration
def test_the_tables_live_in_their_own_schema(schema: str) -> None:
    saver = postgres_checkpointer(DATABASE_URL, schema)
    saver.conn.close()

    with psycopg.connect(DATABASE_URL) as conn:
        rows = conn.execute(
            "select table_name from information_schema.tables where table_schema = %s "
            "order by table_name",
            (schema,),
        ).fetchall()
    assert [r[0] for r in rows] == [
        "checkpoint_blobs",
        "checkpoint_migrations",
        "checkpoint_writes",
        "checkpoints",
    ]


@pytest.mark.integration
def test_a_new_saver_continues_the_thread_of_the_old_one(schema: str) -> None:
    """What a process restart does: new pool, same tables. The migrations run again harmlessly."""
    config = {"configurable": {"thread_id": "t1"}}
    first = postgres_checkpointer(DATABASE_URL, schema)
    counter_graph(first).invoke({"seen": ["turno 1"]}, config)
    first.conn.close()

    second = postgres_checkpointer(DATABASE_URL, schema)
    try:
        graph = counter_graph(second)
        graph.invoke({"seen": ["turno 2"]}, config)
        assert graph.get_state(config).values["seen"] == ["turno 1", "turno 2"]
    finally:
        second.conn.close()


def test_the_schema_must_be_a_plain_identifier() -> None:
    with pytest.raises(ValueError, match="identifier"):
        postgres_checkpointer("postgresql://nobody@localhost/none", "x; drop table y")
