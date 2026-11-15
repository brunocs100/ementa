"""Hybrid search on Postgres: pgvector (HNSW, cosine) and full text (GIN, Portuguese), with RRF.

Accents are folded in Python before `to_tsvector`, on both the document and the query, so the
search does not depend on the `unaccent` extension being installed.
"""

import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from ementa_kit.policy import DataClass
from ementa_kit.rag.fusion import RRF_K

CANDIDATES = 30  # top N of each search before fusion

_SCHEMA = """
create extension if not exists vector;
create schema if not exists {schema};
create table if not exists {schema}.sources (
    id text primary key,
    title text not null,
    url text not null,
    retrieved_at date not null,
    sha256 text not null,
    license text
);
create table if not exists {schema}.chunks (
    id text primary key,
    source_id text not null references {schema}.sources (id) on delete cascade,
    title text not null,
    path text[] not null default '{{}}',
    text text not null,
    provision_ids text[] not null default '{{}}',
    url text not null,
    secrecy_level text not null default 'public',
    acl text[] not null default '{{}}',
    embedding vector({dim}) not null,
    embedding_model text not null,
    doc_sha256 text not null,
    search_text text not null,
    tsv tsvector generated always as (to_tsvector('portuguese', search_text)) stored,
    updated_at timestamptz not null default now()
);
create index if not exists chunks_embedding_hnsw on {schema}.chunks
    using hnsw (embedding vector_cosine_ops);
create index if not exists chunks_tsv_gin on {schema}.chunks using gin (tsv);
create index if not exists chunks_source_id on {schema}.chunks (source_id);
"""

_HYBRID = """
with vec as (
    select id, row_number() over (order by distance, id) as r
    from (
        select id, embedding <=> %(vector)s::vector as distance
        from {schema}.chunks
        order by distance
        limit %(candidates)s
    ) nearest
),
query as (
    select nullif(replace(plainto_tsquery('portuguese', %(text)s)::text, '&', '|'), '')::tsquery
        as tsq
),
txt as (
    select c.id, row_number() over (order by ts_rank_cd(c.tsv, query.tsq) desc, c.id) as r
    from {schema}.chunks c, query
    where c.tsv @@ query.tsq
    order by ts_rank_cd(c.tsv, query.tsq) desc, c.id
    limit %(candidates)s
),
fused as (
    select id, vec.r as vector_rank, txt.r as text_rank,
        coalesce(1.0 / (%(k)s + vec.r), 0) + coalesce(1.0 / (%(k)s + txt.r), 0) as score
    from vec full outer join txt using (id)
)
select c.id, c.source_id, c.title, c.path, c.text, c.provision_ids, c.url,
    f.score::float as score, f.vector_rank, f.text_rank
from fused f join {schema}.chunks c using (id)
order by f.score desc, c.id
limit %(limit)s
"""

_COLUMNS = "id, source_id, title, path, text, provision_ids, url"


def fold(text: str) -> str:
    """Lowercase without accents, for the full-text column and the query."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _vector(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{v:.7f}" for v in values) + "]"


@dataclass(frozen=True)
class Chunk:
    id: str
    source_id: str
    title: str
    path: list[str]
    text: str
    url: str
    doc_sha256: str
    provision_ids: list[str] = field(default_factory=list)
    secrecy_level: DataClass = DataClass.PUBLIC
    acl: list[str] = field(default_factory=list)  # who may read a non-public chunk (RLS, F6)

    def search_text(self) -> str:
        return fold(" ".join([self.title, *self.path, self.text]))


@dataclass(frozen=True)
class Hit:
    id: str
    source_id: str
    title: str
    path: list[str]
    text: str
    provision_ids: list[str]
    url: str
    score: float = 0.0
    vector_rank: int | None = None
    text_rank: int | None = None


class PgHybridStore:
    def __init__(self, database_url: str, dim: int, schema: str = "knowledge") -> None:
        self.database_url = database_url
        self.dim = dim
        self.schema = schema

    def _sql(self, template: str) -> sql.Composed:
        return sql.SQL(template).format(
            schema=sql.Identifier(self.schema), dim=sql.SQL(str(self.dim))
        )

    def _connect(self) -> psycopg.Connection[dict[str, object]]:
        return psycopg.connect(self.database_url, autocommit=True, row_factory=dict_row)

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(self._sql(_SCHEMA))
            row = conn.execute(
                "select atttypmod from pg_attribute where attrelid = %s::regclass "
                "and attname = 'embedding'",
                (f"{self.schema}.chunks",),
            ).fetchone()
            if row is not None and row["atttypmod"] != self.dim:
                raise RuntimeError(
                    f"{self.schema}.chunks has vector({row['atttypmod']}), the embedder gives "
                    f"{self.dim}; drop the schema or run the ingest with --reset"
                )

    def drop_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(self.schema))
            )

    def upsert_source(
        self, id: str, title: str, url: str, retrieved_at: str, sha256: str, license: str | None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                self._sql(
                    "insert into {schema}.sources (id, title, url, retrieved_at, sha256, license) "
                    "values (%s, %s, %s, %s, %s, %s) on conflict (id) do update set "
                    "title = excluded.title, url = excluded.url, "
                    "retrieved_at = excluded.retrieved_at, sha256 = excluded.sha256, "
                    "license = excluded.license"
                ),
                (id, title, url, retrieved_at, sha256, license),
            )

    def replace_chunks(
        self,
        source_id: str,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Sequence[float]],
        embedding_model: str,
    ) -> int:
        """Replace every chunk of a source in one transaction. Returns the number written."""
        if len(chunks) != len(embeddings):
            raise ValueError("one embedding per chunk")
        rows = [
            (
                c.id,
                c.source_id,
                c.title,
                c.path,
                c.text,
                c.provision_ids,
                c.url,
                _vector(e),
                embedding_model,
                c.doc_sha256,
                c.search_text(),
                str(c.secrecy_level),
                c.acl,
            )
            for c, e in zip(chunks, embeddings, strict=True)
        ]
        with self._connect() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(self._sql("delete from {schema}.chunks where source_id = %s"), (source_id,))
            cur.executemany(
                self._sql(
                    "insert into {schema}.chunks (id, source_id, title, path, text, provision_ids, "
                    "url, embedding, embedding_model, doc_sha256, search_text, secrecy_level, acl) "
                    "values (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s, %s, %s)"
                ),
                rows,
            )
        return len(rows)

    def get_many(self, ids: Iterable[str]) -> list[Hit]:
        """Chunks by id, in the order asked; unknown ids are skipped."""
        wanted = list(dict.fromkeys(ids))
        if not wanted:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                self._sql(f"select {_COLUMNS} from {{schema}}.chunks where id = any(%s)"),
                (wanted,),
            ).fetchall()
        by_id = {row["id"]: Hit(**row) for row in rows}  # type: ignore[arg-type]
        return [by_id[i] for i in wanted if i in by_id]

    def hybrid_search(self, text: str, vector: Sequence[float], limit: int = 8) -> list[Hit]:
        with self._connect() as conn:
            rows = conn.execute(
                self._sql(_HYBRID),
                {
                    "vector": _vector(vector),
                    "text": fold(text),
                    "candidates": CANDIDATES,
                    "k": RRF_K,
                    "limit": limit,
                },
            ).fetchall()
        return [Hit(**row) for row in rows]  # type: ignore[arg-type]

    def count(self, source_id: str | None = None) -> int:
        query = "select count(*) as n from {schema}.chunks"
        params: tuple[str, ...] = ()
        if source_id:
            query += " where source_id = %s"
            params = (source_id,)
        with self._connect() as conn:
            row = conn.execute(self._sql(query), params).fetchone()
        return int(row["n"]) if row else 0  # type: ignore[call-overload]
