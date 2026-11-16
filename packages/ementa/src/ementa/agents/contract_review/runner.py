"""`contract_review` behind the approvals runner: what `POST /v1/contracts/{id}/review` starts.

The runner owns the run (the queue, the pause, the resume); this adapter only reads the contract
the route named from the firm's documents, turns it into the graph's input, and the graph's
final state into the run's result.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from langgraph.graph.state import CompiledStateGraph

from ementa.agents.contract_review.graph import AGENT_ID, ContractReviewAgent
from ementa.documents import Document, DocumentRejectedError
from ementa_kit.hitl.runner import DraftingRunError
from ementa_kit.memory.episodes import Episode


class DocumentSource(Protocol):
    """The part of `PgDocumentStore` the adapter reads."""

    def get(self, document_id: str) -> Document | None: ...

    def content(self, document_id: str) -> bytes | None: ...


class ContractReviewRun:
    def __init__(self, agent: ContractReviewAgent, documents: DocumentSource) -> None:
        self.agent = agent
        self.documents = documents

    @property
    def id(self) -> str:
        return AGENT_ID

    @property
    def graph(self) -> CompiledStateGraph[Any, Any, Any, Any]:
        return self.agent.graph

    def example_query(self, request: Mapping[str, Any]) -> str:
        return str(request["document_id"])

    def build_input(
        self, request: Mapping[str, Any], examples: Sequence[Episode]
    ) -> dict[str, Any]:
        """The review takes no examples: the playbook is the firm's wording. A document that is
        gone or cannot be read fails the run here, before any model; another try would read the
        same bytes."""
        document_id = str(request["document_id"])
        document = self.documents.get(document_id)
        content = self.documents.content(document_id)
        if document is None or content is None:
            raise DraftingRunError(f"There is no document {document_id}.")
        try:
            state = self.agent.inputs(
                content,
                document.filename,
                media_type=document.media_type.value,
                document_id=document.id,
                requested_by=request.get("requested_by"),
            )
        except DocumentRejectedError as error:
            raise DraftingRunError(f"The document cannot be reviewed: {error}") from error
        return dict(state)

    def result(self, values: Mapping[str, Any]) -> dict[str, Any]:
        decision = values.get("decision") or {}
        report = self.agent.report(values).model_dump(mode="json")
        return {
            "status": values.get("status"),
            "title": values.get("title"),
            "text": values.get("final"),
            "decided_by": decision.get("decided_by"),
            "comment": decision.get("comment"),
            "matrix": report["matrix"],
            "redlines": report["redlines"],
            "removed": report["removed"],
            "unreviewed": report["unreviewed"],
            "playbook": report["playbook"],
        }
