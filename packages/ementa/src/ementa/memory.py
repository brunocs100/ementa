"""Long-term memory of the firm: a lawyer's preferences, the card of a case and the theses the
firm approved, in the namespaces `("lawyer", id)`, `("case", id)` and `("firm",)`.

A memory follows the permission of its namespace owner. A lawyer's namespace is read and written
only for that lawyer; a case's memories only where the case itself may be seen (RLS from F6); the
firm's theses are read by everyone and written by an approval flow, never by a chat turn.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from langgraph.store.base import BaseStore

from ementa_kit.config import Settings
from ementa_kit.context import Section, approx_tokens
from ementa_kit.memory import Memory, Namespace, search_memories, subject_id

LAWYER = "lawyer"
CASE = "case"
FIRM = Namespace("firm")
EXTRACT_PROMPT = ("memory.extract", 1)
SECTION = "memorias"
SECTION_TOKENS = 600  # budget of the memory section of the context (docs/ARQUITETURA.md)

# For fictitious data only: outside prod, data subject ids fall back to this key.
_DEV_KEY = b"ementa-dev-data-subject-key"
_TITLES = {
    LAWYER: "Preferências do advogado",
    CASE: "Ficha do processo",
    FIRM.kind: "Teses do escritório",
}


def lawyer(user_id: str) -> Namespace:
    return Namespace(LAWYER, user_id)


def case(case_id: str) -> Namespace:
    return Namespace(CASE, case_id)


def chat_targets(user_id: str, case_id: str | None = None) -> list[Namespace]:
    """Where a chat turn may write: the user's own preferences and the card of the case under
    discussion. The firm's theses are left out, since a thesis needs an approval."""
    return [lawyer(user_id), *([case(case_id)] if case_id else [])]


def relevant_memories(
    store: BaseStore,
    query: str,
    *,
    user_id: str,
    case_id: str | None = None,
    per_namespace: int = 3,
) -> list[Memory]:
    """What the assistant reads before answering `query`: the user's preferences, the card of
    the case under discussion and the firm's theses, up to `per_namespace` from each, the closest
    first. Pass `case_id` only for a case the user may see: its memories follow its permission."""
    namespaces = [*chat_targets(user_id, case_id), FIRM]
    return search_memories(store, namespaces, query, per_namespace=per_namespace)


def memory_section(
    memories: Sequence[Memory], *, max_tokens: int = SECTION_TOKENS
) -> Section | None:
    """The `memorias` section of the context: memories grouped by namespace, the closest first
    while they fit in `max_tokens`; None when there is none. The text holds personal data as
    stored: pseudonymize it with the conversation's mapping before a cloud model sees it."""
    kept: list[Memory] = []
    for memory in sorted(memories, key=lambda m: -(m.score or 0.0)):
        fits = approx_tokens(_render([*kept, memory])) <= max_tokens
        if fits and memory.namespace.kind in _TITLES:
            kept.append(memory)
    return Section(SECTION, _render(kept)) if kept else None


def _render(memories: Sequence[Memory]) -> str:
    lines: list[str] = []
    for kind, title in _TITLES.items():
        group = [m.text for m in memories if m.namespace.kind == kind]
        if group:
            lines += [f"{title}:", *(f"- {text}" for text in group)]
    return "\n".join(lines)


def subject_key(settings: Settings) -> bytes:
    """The HMAC key of the data subject ids; prod has to set EMENTA_DATA_SUBJECT_KEY."""
    key = settings.data_subject_key
    if key is not None and key.get_secret_value():
        return key.get_secret_value().encode()
    if settings.is_prod:
        raise RuntimeError("EMENTA_DATA_SUBJECT_KEY is not set; prod needs its own key")
    return _DEV_KEY


@dataclass(frozen=True)
class DataSubject:
    entity: str  # what identifies the person: CPF, PESSOA, EMAIL and the other PII entities
    id: str


def data_subject_ids(text: str, settings: Settings) -> list[DataSubject]:
    """The ids tagging the memories of the people identified in `text`, one per value the PII
    guard finds: "Maria Souza, CPF 123.456.789-09" gives a PESSOA and a CPF. What the DPO looks
    up before `DELETE /v1/data-subjects/{id}/memories`; the values never come back."""
    from ementa.pii import pii_guard  # Presidio and spaCy take seconds to import
    from ementa_kit.guardrails.pii import find_markers

    key = subject_key(settings)
    masked = pii_guard().pseudonymize(text, stage="input")
    return [
        DataSubject(entity, subject_id(key, entity, masked.mapping[marker]))
        for marker, entity in dict.fromkeys(find_markers(masked.text))
    ]
