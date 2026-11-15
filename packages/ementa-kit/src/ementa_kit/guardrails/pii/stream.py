"""Rehydration of a streamed reply.

A model streams its reply in pieces, and a marker can arrive split: "[CP", "F_1]". Rehydrating
each piece alone would show "[CP" and "F_1]" to the user. `MarkerStream` holds back the end of
the text while it may still become a marker and lets it go once it closes or cannot be one, so
what the user sees, joined, is exactly `rehydrate` over the whole reply (a property test checks
it over random splits).
"""

import re
from collections.abc import Mapping

from ementa_kit.guardrails.pii.guard import _parse_marker, rehydrate

# Longest wait for the "]" of a marker; "[" plus a label, a number and some padding fit in it.
MAX_MARKER = 40

_WORD_TAIL = re.compile(r"[\w\\]+$")  # last word, with the backslashes of `CPF\_1`


class MarkerStream:
    """Feed the pieces of one reply; get back the text that is safe to show, values in place.

    `feed` holds back, from the end of the text received so far:

    - an unclosed "[" (or "\\[") up to `MAX_MARKER` characters back, until its "]" arrives;
    - the last word, backslashes included, until a space or punctuation ends it. It may be the
      start of a bare marker ("CP" before "CPF_1", "CPF_1" before "CPF_12"), and a cut inside a
      word would hide from the next piece that a bare marker there is glued to a word.

    So the text comes out word by word. `flush` lets the rest go at the end of the reply.
    Without markers in the mapping nothing is held back.
    """

    def __init__(self, mapping: Mapping[str, str]) -> None:
        for marker in mapping:
            _parse_marker(marker)  # the keys must be markers, as for `rehydrate`
        self._mapping = dict(mapping)
        self._pending = ""

    def feed(self, piece: str) -> str:
        self._pending += piece
        cut = self._hold_from(self._pending)
        ready, self._pending = self._pending[:cut], self._pending[cut:]
        return rehydrate(ready, self._mapping) if ready else ""

    def flush(self) -> str:
        ready, self._pending = self._pending, ""
        return rehydrate(ready, self._mapping) if ready else ""

    def _hold_from(self, text: str) -> int:
        """Where the part that may still become, or touch, a marker starts."""
        if not self._mapping:
            return len(text)
        word = _WORD_TAIL.search(text)
        cut = word.start() if word else len(text)
        opening = text.rfind("[")
        if opening != -1 and "]" not in text[opening:] and len(text) - opening <= MAX_MARKER:
            cut = min(cut, opening - 1 if opening and text[opening - 1] == "\\" else opening)
        return cut
