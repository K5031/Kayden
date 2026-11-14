"""Type-tagged JSON encoding of message blocks.

A block is stored as {"type": tag, "data": {field: value}} with the stable tags of
`api.BLOCK_TYPES`. A field that holds blocks itself (a tool result's attachments) stores
each the same way. A tag this version does not know (or data its class cannot take)
decodes to an `OpaqueBlock`, which re-encodes to exactly what was stored.
"""

from __future__ import annotations

import dataclasses
import json
import typing
from collections.abc import Iterator, Sequence
from enum import Enum
from typing import Any

from kadence.api import BLOCK_TYPES, Block, OpaqueBlock

FORMAT_VERSION = 2  # per-row block encoding: stable BLOCK_TYPES tags

_TAGS = {cls: tag for tag, cls in BLOCK_TYPES.items()}
_HINTS = {tag: typing.get_type_hints(cls) for tag, cls in BLOCK_TYPES.items()}
_SKIP_IN_SEARCH = {"reasoning": ("state", "model"), "tool_call": ("id", "raw_arguments"),
                   "tool_result": ("call_id",), "operation_result": ("call_id",),
                   "server_tool_use": ("id", "state"), "image": ("data", "media_type"),
                   "document": ("data",)}


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
    return {"type": tag, "data": {f.name: _encode_value(getattr(block, f.name))
                                  for f in dataclasses.fields(block)}}


def _encode_value(value: Any) -> Any:
    if isinstance(value, (list, tuple)) and value and all(type(v) in _TAGS for v in value):
        return [encode_block(v) for v in value]
    return list(value) if isinstance(value, tuple) else value


def _holds_blocks(hint: Any) -> bool:
    """Whether a field's type is a sequence of blocks."""
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if not (isinstance(origin, type) and issubclass(origin, Sequence) and len(args) == 1):
        return False
    members = typing.get_args(args[0]) or (args[0],)
    return all(m in _TAGS for m in members)


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
            if isinstance(hint, type) and issubclass(hint, Enum):
                value = hint(value)
            elif _holds_blocks(hint):
                value = tuple(decode_block(v) for v in value)
            kwargs[name] = value
        return cls(**kwargs)
    except (KeyError, TypeError, ValueError):
        return OpaqueBlock(tag, data)  # e.g. a field added by a later version


def searchable_text(raw: str) -> str:
    """The string values of the encoded blocks, minus ids and opaque provider state."""
    return "\n".join(part for item in json.loads(raw) for part in _strings(item))


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, dict):
        if value.keys() == {"type", "data"} and isinstance(value["data"], dict) \
                and value["type"] in _SKIP_IN_SEARCH:  # a block: without what it skips
            skip = _SKIP_IN_SEARCH[value["type"]]
            value = {k: v for k, v in value["data"].items() if k not in skip}
        elif value.keys() == {"type", "data"} and value["type"] in BLOCK_TYPES:
            value = value["data"]
        for v in value.values() if isinstance(value, dict) else ():
            yield from _strings(v)
