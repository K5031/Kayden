"""Generic, type-tagged JSON encoding of message blocks.

A block is any module-level frozen dataclass. It is stored as
{"type": "<module>:<qualname>", "data": {field: value}}, so block types added later
round-trip without a storage migration.
"""

from __future__ import annotations

import dataclasses
import functools
import importlib
import json
import typing
from collections.abc import Iterator, Sequence
from enum import Enum
from typing import Any

from kayden.api import Block, Reasoning

FORMAT_VERSION = 1


def encode_blocks(blocks: Sequence[Block]) -> str:
    return json.dumps([_encode_block(b) for b in blocks], ensure_ascii=False)


def decode_blocks(raw: str) -> tuple[Block, ...]:
    return tuple(_decode_block(item) for item in json.loads(raw))


def searchable_text(raw: str) -> str:
    """All string values of the encoded blocks, minus opaque provider state."""
    parts: list[str] = []
    for item in json.loads(raw):
        data = item["data"]
        parts.extend(_strings(data.get("text") if item["type"] == _REASONING_TAG else data))
    return "\n".join(parts)


def _encode_block(block: Block) -> dict[str, Any]:
    cls = type(block)
    if not dataclasses.is_dataclass(block):
        raise TypeError(f"block must be a dataclass instance, got {cls.__name__}")
    tag = f"{cls.__module__}:{cls.__qualname__}"
    if _resolve(tag)[0] is not cls:
        raise TypeError(f"block type {tag} is not importable by its module path")
    data = {f.name: getattr(block, f.name) for f in dataclasses.fields(block)}
    return {"type": tag, "data": data}


def _decode_block(item: dict[str, Any]) -> Block:
    cls, hints = _resolve(item["type"])
    kwargs = {}
    for name, value in item["data"].items():
        hint = hints.get(name)
        if isinstance(hint, type) and issubclass(hint, Enum):
            value = hint(value)
        kwargs[name] = value
    return cls(**kwargs)


@functools.cache
def _resolve(tag: str) -> tuple[type, dict[str, Any]]:
    module_name, _, qualname = tag.partition(":")
    try:
        obj: Any = importlib.import_module(module_name)
        for part in qualname.split("."):
            obj = getattr(obj, part)
    except (ImportError, AttributeError) as e:
        raise LookupError(f"unknown block type {tag}") from e
    if not (isinstance(obj, type) and dataclasses.is_dataclass(obj)):
        raise LookupError(f"block type {tag} is not a dataclass")
    try:
        hints = typing.get_type_hints(obj)
    except (NameError, TypeError):
        hints = {}
    return obj, hints


_REASONING_TAG = f"{Reasoning.__module__}:{Reasoning.__qualname__}"


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
