"""Post-run memory writer: what a finished turn taught, extracted by the `fast` model from
pseudonymized text, deduplicated against the stored memories and tagged with the data subjects it
mentions.

The model never sees personal data. The turn goes through the PII guard first, the model writes
markers (`[PESSOA_1]`), and the markers go back to their values only for the store, on this
machine. Each memory carries the ids of the people behind its values, so forgetting a person
finds every memory about them.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.store.base import BaseStore
from pydantic import BaseModel, Field, ValidationError

from ementa_kit.context import Section
from ementa_kit.llm import ModelRegistry, ModelRole
from ementa_kit.memory.store import SUBJECTS, Namespace, memory_value, subject_id
from ementa_kit.prompts import Prompt
from ementa_kit.telemetry import get_logger, get_tracer

if TYPE_CHECKING:
    from ementa_kit.guardrails.pii import PiiGuard

MAX_MEMORIES = 5  # kept from one turn
MAX_CHARS = 400  # per memory

# The model reads the turn with its speakers named in Portuguese, the language of the prompts.
_SPEAKERS = {"human": "usuário", "ai": "assistente"}
_FIX = (
    "A resposta anterior não é um JSON no formato pedido. "
    'Responda de novo só com o JSON: {"memories": [...]}.'
)

tracer = get_tracer(__name__)
log = get_logger(__name__)


class Candidate(BaseModel):
    """A memory the model proposes: the namespace kind, one self-contained sentence and how much
    the turn supports it."""

    scope: str
    text: str = Field(min_length=1, max_length=MAX_CHARS)
    confidence: float = Field(ge=0, le=1)


class _Reply(BaseModel):
    memories: list[dict[str, Any]]


@dataclass(frozen=True)
class Written:
    namespace: Namespace
    key: str
    updated: bool  # it replaced a stored memory that said nearly the same about the same people


class MemoryWriter:
    """Saves what a finished turn taught into the namespaces the caller allows.

    `prompt` tells the `fast` model what is worth keeping in each namespace kind and to answer
    `{"memories": [{"scope": <kind>, "text": ..., "confidence": <0 to 1>}]}`. A candidate under
    `min_confidence` is dropped; one at least `similarity` alike to a stored memory about the
    same people replaces it instead of adding a near copy.
    """

    def __init__(
        self,
        store: BaseStore,
        models: ModelRegistry,
        guard: Callable[[], "PiiGuard"],
        prompt: Prompt,
        *,
        subject_key: bytes,
        similarity: float,
        min_confidence: float,
    ) -> None:
        self.store = store
        self.models = models
        self.guard = guard
        self.prompt = prompt
        self.subject_key = subject_key
        self.similarity = similarity
        self.min_confidence = min_confidence

    def write(
        self,
        turn: Sequence[BaseMessage],
        targets: Sequence[Namespace],
        mapping: Mapping[str, str] | None = None,
    ) -> list[Written]:
        """Extract, check and save the memories of `turn`: the user's messages and the
        assistant's replies (system and tool messages are left out). A memory goes only to a
        namespace in `targets`, matched by kind, so the caller decides where this user may write.
        Pass the conversation's marker `mapping`, if there is one, to keep its markers.
        """
        from ementa_kit.guardrails.pii import find_markers, rehydrate

        text = _render(turn)
        allowed = {namespace.kind: namespace for namespace in targets}
        if not text or not allowed:
            return []
        with tracer.start_as_current_span(
            "memory write",
            attributes={
                "ementa.memory.operation": "write",
                "ementa.prompt.id": self.prompt.id,
                "ementa.prompt.version": self.prompt.version,
            },
        ) as span:
            guard = self.guard()
            masked = guard.pseudonymize(text, mapping, stage="input")
            known = masked.mapping
            candidates = self._extract(masked.text, list(allowed))
            written: list[Written] = []
            for candidate in candidates:
                namespace = allowed.get(candidate.scope)
                if namespace is None or candidate.confidence < self.min_confidence:
                    continue
                memory = rehydrate(candidate.text, known)
                if find_markers(memory):  # a marker the model made up stands for no one
                    continue
                # A value the guard missed in the turn is caught here, so it is tagged as well.
                check = guard.pseudonymize(memory, known, stage="output")
                known = check.mapping
                subjects = sorted(
                    {
                        subject_id(self.subject_key, entity, known[marker])
                        for marker, entity in find_markers(check.text)
                    }
                )
                written.append(self._save(namespace, memory, subjects, candidate.confidence))
            span.set_attribute("ementa.memory.candidates", len(candidates))
            span.set_attribute("ementa.memory.inserted", sum(not w.updated for w in written))
            span.set_attribute("ementa.memory.updated", sum(w.updated for w in written))
            return written

    def _extract(self, turn: str, kinds: list[str]) -> list[Candidate]:
        model = self.models.chat(ModelRole.FAST)
        sections = [Section("escopos", ", ".join(kinds)), Section("turno", turn)]
        request: list[BaseMessage] = [
            SystemMessage(self.prompt.text),
            HumanMessage("\n\n".join(s.render() for s in sections)),
        ]
        reply = model.invoke(request)
        items = _parse(reply.text)
        if items is None:  # one more try: the reply stays and the correction goes after it
            reply = model.invoke([*request, reply, HumanMessage(_FIX)])
            items = _parse(reply.text)
        if items is None:
            log.warning("memory_extraction_rejected", prompt=self.prompt.id)
            return []
        candidates: list[Candidate] = []
        for item in items:
            try:
                candidates.append(Candidate.model_validate(item))
            except ValidationError:
                continue  # one bad item does not cost the others
        return candidates[:MAX_MEMORIES]

    def _save(
        self, namespace: Namespace, text: str, subjects: list[str], confidence: float
    ) -> Written:
        """Replace the closest memory about exactly the same people when it is at least
        `similarity` alike; otherwise add one. Different people never merge: on E5, "João wants
        a settlement" and "Maria wants a settlement" are closer than many rewrites of one fact."""
        near = self.store.search(namespace.path, query=text, filter={SUBJECTS: subjects}, limit=1)
        same = next(
            (
                item
                for item in near
                if item.namespace == namespace.path and (item.score or 0.0) >= self.similarity
            ),
            None,
        )
        key = same.key if same else uuid4().hex
        self.store.put(namespace.path, key, memory_value(namespace, text, subjects, confidence))
        return Written(namespace, key, updated=same is not None)


def _render(turn: Sequence[BaseMessage]) -> str:
    return "\n\n".join(
        f"{_SPEAKERS[m.type]}: {m.text.strip()}"
        for m in turn
        if m.type in _SPEAKERS and m.text.strip()
    )


def _parse(text: str) -> list[dict[str, Any]] | None:
    raw = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        return _Reply.model_validate_json(raw).memories
    except ValidationError:
        return None
