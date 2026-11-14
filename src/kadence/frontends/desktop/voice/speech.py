"""What of a reply is read aloud: its words, sentence by sentence while it streams."""

from __future__ import annotations

import re

_FENCE = re.compile(r"```.*?(```|\Z)", re.DOTALL)
_TABLE_ROW = re.compile(r"^\s*\|.*$", re.MULTILINE)
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"\b(?:https?://|www\.)\S+")
_LINE_START = re.compile(r"^\s*(?:#{1,6}|>+|[-*+]|\d+[.)])\s+", re.MULTILINE)
_MARKS = re.compile(r"[*_`~]+")
# The English voice reads Latin letters, digits and ordinary punctuation; anything else
# (symbols, emoji, other scripts) it would spell out or stumble over.
_UNSPEAKABLE = re.compile(r"[^\w\s.,;:!?'\"()%$&/+\-]|[^\x00-ɏ]|_")
_EMPTY_BRACKETS = re.compile(r"\([^\w()]*\)")
_LOOSE = re.compile(r"(?<!\S)[^\w\s]+(?!\S)")  # punctuation left standing alone
_SPACE = re.compile(r"\s+")
_SPACE_BEFORE = re.compile(r"\s+([.,;:!?)])")
# A sentence ends at ., ! or ? before a space (not after a digit: "3. item"), or at a line.
_END = re.compile(r"(?<=[^\d\s][.!?])\s+|\n+")


def spoken(text: str) -> str:
    """`text` (Markdown) as it is read aloud: no code blocks, tables, links' addresses,
    markup or symbols that are not words; "" when nothing is left to say."""
    text = _TABLE_ROW.sub("", _FENCE.sub(" ", text))
    text = _URL.sub("", _LINK.sub(r"\1", text))
    text = _MARKS.sub("", _LINE_START.sub("", text))
    text = _UNSPEAKABLE.sub(" ", text)
    for _ in range(2):  # brackets that held only symbols, then what stood around them
        text = _EMPTY_BRACKETS.sub(" ", text)
    text = _LOOSE.sub(" ", text)
    text = _SPACE_BEFORE.sub(r"\1", _SPACE.sub(" ", text)).strip()
    return text if any(c.isalnum() for c in text) else ""


class Sentences:
    """Cuts a streaming reply into what to say next, so speech starts before the reply
    has ended. A code block is waited out whole."""

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, text: str) -> list[str]:
        self._pending += text
        if self._pending.count("```") % 2:  # inside a code block
            return []
        *whole, self._pending = _END.split(_FENCE.sub("\n", self._pending))
        return [said for part in whole if (said := spoken(part))]

    def flush(self) -> list[str]:
        """The reply ended: what is left of it."""
        rest, self._pending = spoken(self._pending), ""
        return [rest] if rest else []
