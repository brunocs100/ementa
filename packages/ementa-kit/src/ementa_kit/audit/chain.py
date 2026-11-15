"""The hash chain of audit events, without storage.

Each record keeps the hash of the record before it (`prev_hash`, none for the first) and its own
`hash = sha256(prev_hash + canonical(event))`. The canonical form is the event as JSON with
sorted keys, no spaces, UTF-8 text and the time in UTC with microseconds. Changing a record
breaks its own hash; removing or reordering records breaks the `prev_hash` of the next one.
`verify_chain` reports the first record where either happens.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class Outcome(StrEnum):
    OK = "ok"
    DENIED = "denied"  # authentication or authorization refused
    REJECTED = "rejected"  # the request itself was invalid
    ERROR = "error"  # a failure on our side


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class AuditEvent:
    """What happened. `details` holds small facts such as a status code, never personal data."""

    actor: str | None  # user id or service name; None when nobody authenticated
    action: str  # "http.request", "tool.call"
    resource: str  # "POST /v1/legal-research", "calculate_deadline"
    data_class: str  # a `DataClass` value: public, restricted or secret
    outcome: str  # an `Outcome` value
    details: Mapping[str, object] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class AuditRecord:
    id: int
    event: AuditEvent
    prev_hash: bytes | None
    hash: bytes


def canonical(event: AuditEvent) -> bytes:
    document = {
        "occurred_at": event.occurred_at.astimezone(UTC).isoformat(timespec="microseconds"),
        "actor": event.actor,
        "action": event.action,
        "resource": event.resource,
        "data_class": str(event.data_class),
        "outcome": str(event.outcome),
        "details": event.details,
    }
    text = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text.encode("utf-8")


def chain_hash(prev_hash: bytes | None, event: AuditEvent) -> bytes:
    return hashlib.sha256((prev_hash or b"") + canonical(event)).digest()


def check_event(event: AuditEvent) -> None:
    """Refuse, before writing, what storage would not give back unchanged: such an event would
    fail verification later and look like tampering."""
    if event.occurred_at.tzinfo is None:
        raise ValueError("occurred_at needs a time zone")
    _check_json(event.details)


def _check_json(value: object) -> None:
    if isinstance(value, float):
        raise TypeError("audit details take no floats (jsonb rewrites numbers); use int or str")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("audit detail keys must be strings")
            _check_json(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _check_json(item)
    elif value is not None and not isinstance(value, str | int):  # bool is an int
        raise TypeError(f"audit details take JSON values, not {type(value).__name__}")


@dataclass(frozen=True)
class BrokenLink:
    id: int
    reason: str


@dataclass(frozen=True)
class ChainCheck:
    checked: int  # records verified before the break, or all of them
    head: bytes | None  # hash of the last record that verified
    broken: BrokenLink | None = None

    @property
    def intact(self) -> bool:
        return self.broken is None


LINK_BROKEN = "prev_hash does not match the record before it: a record was removed or rewritten"
CONTENT_CHANGED = "hash does not match the content: the record was changed"


def verify_chain(records: Iterable[AuditRecord]) -> ChainCheck:
    """Walk the records in chain order and stop at the first broken link."""
    checked = 0
    head: bytes | None = None
    for record in records:
        if record.prev_hash != head:
            return ChainCheck(checked, head, BrokenLink(record.id, LINK_BROKEN))
        if record.hash != chain_hash(record.prev_hash, record.event):
            return ChainCheck(checked, head, BrokenLink(record.id, CONTENT_CHANGED))
        head = record.hash
        checked += 1
    return ChainCheck(checked, head)
