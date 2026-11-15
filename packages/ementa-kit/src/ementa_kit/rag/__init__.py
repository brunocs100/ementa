"""Retrieval: local embeddings and hybrid search (vector plus full text) on Postgres."""

from ementa_kit.rag.embeddings import Embedder, FastEmbedder
from ementa_kit.rag.fusion import rrf_fuse
from ementa_kit.rag.store import Chunk, Hit, PgHybridStore, fold

__all__ = ["Chunk", "Embedder", "FastEmbedder", "Hit", "PgHybridStore", "fold", "rrf_fuse"]
