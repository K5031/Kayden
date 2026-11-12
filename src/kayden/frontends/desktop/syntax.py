"""Syntax colours for the code blocks of a reply, from Pygments."""

from __future__ import annotations

from functools import cache

from pygments.lexer import Lexer
from pygments.lexers import get_lexer_by_name
from pygments.token import Comment, Keyword, Name, Number, String, _TokenType
from pygments.util import ClassNotFound

# The kinds of token that get a colour, most specific first; the rest keep the text's.
_KINDS: tuple[tuple[_TokenType, str], ...] = (
    (Comment, "comment"),
    (Keyword, "keyword"),
    (String, "string"),
    (Number, "number"),
    (Name.Function, "name"),
    (Name.Class, "name"),
    (Name.Decorator, "name"),
    (Name.Tag, "keyword"),
    (Name.Attribute, "name"),
    (Name.Builtin, "builtin"),
)
_LIGHT = {"keyword": "#6B3FC4", "string": "#1F7A3D", "comment": "#7A7A86", "number": "#A8500F",
          "name": "#1F5FBF", "builtin": "#0F7A7A"}
_DARK = {"keyword": "#C4A7F5", "string": "#9FD3A2", "comment": "#8A8A96", "number": "#F0B07A",
         "name": "#8FB8F5", "builtin": "#7FD1D1"}


def colors(dark: bool) -> dict[str, str]:
    """The colour of each kind of token, on a code block's background."""
    return _DARK if dark else _LIGHT


@cache
def _lexer(language: str) -> Lexer | None:
    try:
        return get_lexer_by_name(language)
    except ClassNotFound:
        return None


def spans(code: str, language: str) -> list[tuple[int, int, str]]:
    """(start, length, kind) of each piece of `code` that has a colour; none when the
    language is not named or not known: a guess would colour it wrongly."""
    lexer = _lexer(language.strip().lower()) if language.strip() else None
    if lexer is None:
        return []
    found = []
    # With a line end, as Pygments itself adds one: some rules need it (a shell comment).
    for start, token, value in lexer.get_tokens_unprocessed(code.rstrip("\n") + "\n"):
        kind = next((kind for parent, kind in _KINDS if token in parent), None)
        if kind is not None and value.strip():
            found.append((start, len(value.rstrip("\n")), kind))
    return found
