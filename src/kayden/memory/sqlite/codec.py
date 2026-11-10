"""Type-tagged JSON encoding of message blocks.

A block is stored as {"type": tag, "data": {field: value}} with the stable tags of
`api.BLOCK_TYPES`. A tag this version does not know (or data its class cannot take)
decodes to an `OpaqueBlock`, which re-encodes to exactly what was stored.
"""

from __future__ import annotations

import dataclasses
import json
import typing
from collections.abc import Iterator, Sequence
from enum import Enum
from typing import Any

from kayden.api import BLOCK_TYPES, Block, OpaqueBlock

FORMAT_VERSION = 2  # per-row block encoding: stable BLOCK_TYPES tags

_TAGS = {cls: tag for tag, cls in BLOCK_TYPES.items()}
_HINTS = {tag: typing.get_type_hints(cls) for tag, cls in BLOCK_TYPES.items()}
_SKIP_IN_SEARCH = {"reasoning": ("state", "model"), "tool_call": ("id", "raw_arguments"),
                   "tool_result": ("call_id",), "operation_result": ("call_id",),
                   "server_tool_use": ("id", "state")}


def encode_blocks(blocks: Sequence[Block]) -> str:
    return json.dumps([encode_block(b) for b in blocks], ensure_ascii=False)


def decode_blocks(raw: str) -> tuple[Block, ...]:
    return tuple(decode_block(item) for item in json.loads(raw))


def encode_block(block: Block) -> dict[str, Any]:
    if isinstance(block, OpaqueBlock):
        return {"type": block.type, "data": block.data}
    tag = _TAGS.get(type(block))
    if tag is None:
        raise TypeError(f"{type(block).__name__} has no tag in BLOCK_TYPES")
    return {"type": tag, "data": {f.name: getattr(block, f.name)
                                  for f in dataclasses.fields(block)}}


def decode_block(item: dict[str, Any]) -> Block:
    tag, data = item["type"], item["data"]
    cls = BLOCK_TYPES.get(tag)
    if cls is None:
        return OpaqueBlock(tag, data)
    hints = _HINTS[tag]
    try:
        kwargs = {}
        for name, value in data.items():
            hint = hints[name]
            kwargs[name] = hint(value) if isinstance(hint, type) and issubclass(hint, Enum) \
                else value
        return cls(**kwargs)
    except (KeyError, TypeError, ValueError):
        return OpaqueBlock(tag, data)  # e.g. a field added by a later version


def searchable_text(raw: str) -> str:
    """The string values of the encoded blocks, minus ids and opaque provider state."""
    parts: list[str] = []
    for item in json.loads(raw):
        skip = _SKIP_IN_SEARCH.get(item["type"], ())
        data = item["data"]
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if k not in skip}
        parts.extend(_strings(data))
    return "\n".join(parts)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
