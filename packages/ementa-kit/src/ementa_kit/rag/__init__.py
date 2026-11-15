"""Retrieval: local embeddings, hybrid search (vector plus full text) on Postgres and rerank."""

from ementa_kit.rag.embeddings import Embedder, FastEmbedder
from ementa_kit.rag.fusion import rrf_fuse
from ementa_kit.rag.rerank import FastReranker, Reranker
from ementa_kit.rag.store import Chunk, Hit, PgHybridStore, fold

__all__ = [
    "Chunk",
    "Embedder",
    "FastEmbedder",
    "FastReranker",
    "Hit",
    "PgHybridStore",
    "Reranker",
    "fold",
    "rrf_fuse",
]
