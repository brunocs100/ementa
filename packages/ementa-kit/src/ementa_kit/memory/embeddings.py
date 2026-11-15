"""The kit's local embedder as LangChain `Embeddings`, for the vector index of the store."""

from langchain_core.embeddings import Embeddings

from ementa_kit.rag import Embedder


class LocalEmbeddings(Embeddings):
    """Memories are embedded on this machine, like the corpus (ADR 0010).

    Both sides go through the query side of the embedder. The Postgres store embeds what it saves
    and what it searches with the same call (`embed_documents`), and E5 asks for the `query: `
    prefix on both texts of a symmetric task, such as telling whether two memories say the same.
    """

    def __init__(self, embedder: Embedder) -> None:
        self.embedder = embedder

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embedder.embed_queries(texts)

    def embed_query(self, text: str) -> list[float]:
        return self.embedder.embed_queries([text])[0]
