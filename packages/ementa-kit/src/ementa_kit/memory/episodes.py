"""Episodic memory: the drafts lawyers approved or edited, kept as few-shot examples for the
agent's next runs (F4).

An episode is one store item under `("episodes", <agent>)`, keyed by the approval that produced
it: the draft as the agent wrote it, the text as the lawyer approved it and the lawyer's edit as
a unified diff. Personal data is swapped for markers (`[PESSOA_1]`) before it is stored. An
episode from one client's case becomes an example in another client's case, where those names
have no business, and a model shown an example tends to copy from it; what an example teaches is
the shape of the text, which the markers keep. The ids of the people behind the markers tag the
episode as they tag every memory, so forgetting a person (LGPD, art. 18) deletes it too.

```python
episodes = Episodes(store, guard, subject_key=key)
episodes.record(approval_id, agent="brief_drafting", kind="brief", title=..., draft=..., final=...)
examples = episodes.examples_for("brief_drafting", facts, k=3)  # the closest first
```
"""

import difflib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from langgraph.store.base import BaseStore, SearchItem

from ementa_kit.memory.store import Namespace, memory_value, subject_id
from ementa_kit.telemetry import get_tracer

if TYPE_CHECKING:  # Presidio and spaCy take seconds to import; only writing needs them
    from ementa_kit.guardrails.pii import PiiGuard

EPISODES = "episodes"
EXAMPLES = 3  # past episodes a run gets when the caller does not say

tracer = get_tracer(__name__)


def episodes_of(agent: str) -> Namespace:
    """Where the episodes of `agent` live: `("episodes", agent)`."""
    return Namespace(EPISODES, agent)


def edit_diff(draft: str, final: str) -> str:
    """The lawyer's edit as a unified diff, line by line with one line of context around each
    change; empty when the text did not change."""
    lines = difflib.unified_diff(
        draft.splitlines(), final.splitlines(), "rascunho", "final", n=1, lineterm=""
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class Episode:
    """A draft and what the lawyer made of it, with personal data as markers."""

    key: str  # the approval that produced it
    agent: str
    kind: str
    title: str
    draft: str  # as the agent wrote it
    final: str  # as the lawyer approved it
    diff: str  # from draft to final; empty when approved as written
    action: str  # approve or edit
    comment: str | None
    updated_at: datetime
    score: float | None = None  # similarity to the query, in `examples_for`


class Episodes:
    """The episodic memory of the drafting agents, on the long-term memory store.

    `guard` builds the PII guard on first use (it loads spaCy); `subject_key` is the HMAC key of
    the data subject ids, the same the memory writer uses, so one forgetting reaches both.
    """

    def __init__(
        self, store: BaseStore, guard: Callable[[], "PiiGuard"], *, subject_key: bytes
    ) -> None:
        self.store = store
        self.guard = guard
        self.subject_key = subject_key

    def record(
        self,
        key: str,
        *,
        agent: str,
        kind: str,
        title: str,
        draft: str,
        final: str,
        action: str = "approve",
        comment: str | None = None,
        decided_by: str | None = None,
    ) -> Episode:
        """Keep an approved (`final` is the draft) or edited draft as an example for `agent`.

        `key` is the approval id: recording the same approval again replaces the episode, so a
        retried job does not add a copy.
        """
        from ementa_kit.guardrails.pii import find_markers

        namespace = episodes_of(agent)
        with tracer.start_as_current_span(
            "memory write",
            attributes={"ementa.memory.operation": "episode", "gen_ai.agent.name": agent},
        ):
            guard = self.guard()
            mapping: dict[str, str] = {}
            masked: dict[str, str] = {}
            # One mapping for the four texts: a name gets the same marker in the draft and in
            # the final text, so the diff shows only what the lawyer changed.
            for field, text in (("title", title), ("draft", draft), ("final", final)):
                result = guard.pseudonymize(text, mapping, stage="input")
                mapping, masked[field] = result.mapping, result.text
            note = guard.pseudonymize(comment, mapping, stage="input") if comment else None
            if note is not None:
                mapping = note.mapping
            subjects = sorted(
                {
                    subject_id(self.subject_key, entity, mapping[marker])
                    for marker, entity in find_markers(" ".join(mapping))
                }
            )
            value: dict[str, Any] = {
                **memory_value(namespace, f"{masked['title']}\n\n{masked['final']}", subjects, 1.0),
                "agent": agent,
                "kind": kind,
                "title": masked["title"],
                "draft": masked["draft"],
                "final": masked["final"],
                "diff": edit_diff(masked["draft"], masked["final"]),
                "action": action,
                "comment": note.text if note is not None else None,
                "decided_by": decided_by,
            }
            self.store.put(namespace.path, key, value)
        item = self.store.get(namespace.path, key)
        assert item is not None  # just written
        return _episode(item.key, item.value, item.updated_at)

    def examples_for(self, agent: str, query: str, k: int = EXAMPLES) -> list[Episode]:
        """Up to `k` past episodes of `agent`, the closest to `query` first: few-shot examples
        for the agent's `examples` input. The search runs on the local embedder; the episodes
        hold markers, never the personal data of the case they came from."""
        if k <= 0:
            return []
        namespace = episodes_of(agent)
        with tracer.start_as_current_span(
            "retrieval memory",
            attributes={
                "gen_ai.operation.name": "retrieval",
                "gen_ai.data_source.id": EPISODES,
                "gen_ai.agent.name": agent,
            },
        ) as span:
            found = [
                _found(item)
                for item in self.store.search(namespace.path, query=query, limit=k)
                if item.namespace == namespace.path and "final" in item.value
            ]
            span.set_attribute("ementa.memory.hits", len(found))
            return found


def _episode(
    key: str, value: dict[str, Any], updated_at: datetime, score: float | None = None
) -> Episode:
    return Episode(
        key=key,
        agent=str(value["agent"]),
        kind=str(value["kind"]),
        title=str(value["title"]),
        draft=str(value["draft"]),
        final=str(value["final"]),
        diff=str(value["diff"]),
        action=str(value["action"]),
        comment=value.get("comment"),
        updated_at=updated_at,
        score=score,
    )


def _found(item: SearchItem) -> Episode:
    return _episode(item.key, item.value, item.updated_at, item.score)
