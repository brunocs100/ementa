"""`brief_drafting` behind the approvals runner: what `POST /v1/briefs` starts.

The runner owns the run (the queue, the pause, the resume); this adapter only turns the route's
request into the graph's input and the graph's final state into the run's result.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from langgraph.graph.state import CompiledStateGraph

from ementa.agents.brief_drafting.graph import AGENT_ID, BriefDraftingAgent, ExampleRecord, State
from ementa_kit.memory.episodes import Episode

DEFAULT_TEMPLATE = "initial-petition-credit-listing"


class BriefDraftingRun:
    def __init__(self, agent: BriefDraftingAgent) -> None:
        self.agent = agent

    @property
    def id(self) -> str:
        return AGENT_ID

    @property
    def graph(self) -> CompiledStateGraph[Any, Any, Any, Any]:
        return self.agent.graph

    def example_query(self, request: Mapping[str, Any]) -> str:
        return str(request["facts"])

    def build_input(
        self, request: Mapping[str, Any], examples: Sequence[Episode]
    ) -> dict[str, Any]:
        """An unknown template raises here, so the run fails with the reason before any model."""
        template = str(request.get("template") or DEFAULT_TEMPLATE)
        self.agent.library.template(template)
        state = State(
            template=template,
            description=str(request["facts"]),
            examples=[
                ExampleRecord(draft=e.draft, final=e.final, comment=e.comment) for e in examples
            ],
            owner_id=str(request.get("requested_by", "")),
        )
        return dict(state)

    def result(self, values: Mapping[str, Any]) -> dict[str, Any]:
        outcome = values.get("outcome") or {}
        review = values.get("review") or {}
        check = values.get("check") or {}
        return {
            "status": outcome.get("status"),
            "title": review.get("title"),
            "text": outcome.get("text"),
            "decided_by": outcome.get("decided_by"),
            "decided_at": outcome.get("decided_at"),
            "comment": outcome.get("comment"),
            "citations": sorted(check.get("valid", [])),
            "removed": list(check.get("removed", [])),
        }
