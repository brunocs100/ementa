"""Append-only check of the requests sent to a model.

The provider caches the longest prefix it has already seen, and some providers refuse a history
whose earlier turns changed. So each request must start with the whole previous request,
unchanged: new context goes in as new messages at the end.
"""

from collections.abc import Sequence
from itertools import pairwise

from langchain_core.messages import BaseMessage


class PrefixChangedError(AssertionError):
    """A request did not start with the previous request of the same conversation."""


def assert_append_only(requests: Sequence[Sequence[BaseMessage]]) -> None:
    """Raise PrefixChangedError when a request edits, drops or reorders a message that the
    previous request sent. `requests` are the message lists sent to one model, in order, as a
    recording fake model keeps them."""
    for number, (before, after) in enumerate(pairwise(requests), start=2):
        for index, old in enumerate(before):
            if index >= len(after):
                raise PrefixChangedError(
                    f"request {number} dropped message {index} ({old.type}) of request {number - 1}"
                )
            new = after[index]
            if new != old:
                raise PrefixChangedError(
                    f"request {number} changed message {index} of request {number - 1}: "
                    f"{_difference(old, new)}"
                )


def _difference(old: BaseMessage, new: BaseMessage, width: int = 40) -> str:
    if old.type != new.type:
        return f"{old.type} became {new.type}"
    if old.text == new.text:
        return "same text, other fields differ"
    pairs = zip(old.text, new.text, strict=False)
    at = next((i for i, (a, b) in enumerate(pairs) if a != b), min(len(old.text), len(new.text)))
    return (
        f"{old.type} text differs from character {at}: "
        f"{old.text[at : at + width]!r} became {new.text[at : at + width]!r}"
    )
