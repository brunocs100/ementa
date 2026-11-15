"""Short-term memory: LangGraph checkpoints in Postgres, so a conversation survives a restart.

The tables are LangGraph's own (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes` and
`checkpoint_migrations`), created by its migrations in a schema of their own through the
connections' `search_path`. Every checkpoint of a thread is kept, not only the last one: the full
state stays for audit, and what goes to the model is derived from it.
"""

import re

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection, sql
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

CHECKPOINT_SCHEMA = "langgraph"

_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def postgres_checkpointer(
    database_url: str, schema: str = CHECKPOINT_SCHEMA, *, max_size: int = 4
) -> PostgresSaver:
    """A saver on a pool of up to `max_size` connections whose tables live in `schema`.

    It creates the schema and runs LangGraph's migrations, so build it once per process and
    close it with `saver.conn.close()`. The pool checks a connection before lending it: after a
    database restart, the next call reconnects instead of failing.
    """
    if not _IDENTIFIER.fullmatch(schema):  # it goes into the connection options
        raise ValueError(f"schema must be a lower-case SQL identifier, not {schema!r}")
    with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as conn:
        conn.execute(sql.SQL("create schema if not exists {}").format(sql.Identifier(schema)))
    pool = ConnectionPool[Connection[DictRow]](
        database_url,
        kwargs={
            "autocommit": True,  # the migrations create their indexes concurrently
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "options": f"-c search_path={schema}",
        },
        min_size=1,
        max_size=max_size,
        check=ConnectionPool.check_connection,
        open=True,
    )
    saver = PostgresSaver(pool)
    try:
        saver.setup()
    except BaseException:
        pool.close()
        raise
    return saver
