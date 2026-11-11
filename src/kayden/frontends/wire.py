"""How API values travel over the headless channel: dataclasses as JSON objects with their
class name under "type", enums as their values, tuples as lists. from_json reverses it,
guided by the API's type hints, so a dict that only looks like an object (a tool call's
arguments) stays a dict."""

from __future__ import annotations

import dataclasses
import types
import typing
from collections.abc import Mapping, Sequence
from enum import Enum
from functools import cache
from pathlib import Path
from typing import Any

from kayden import api

CLASSES: Mapping[str, type] = {
    name: cls for name, cls in vars(api).items()
    if isinstance(cls, type) and dataclasses.is_dataclass(cls)}


def to_json(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {"type": type(value).__name__,
                **{f.name: to_json(getattr(value, f.name)) for f in dataclasses.fields(value)}}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


def from_json(value: Any, hint: Any = Any) -> Any:
    """`value` as the API type `hint` names; with no hint, an object with a known "type"
    becomes that class (an event, a snapshot)."""
    if value is None:
        return None
    if isinstance(hint, typing.TypeAliasType):
        return from_json(value, hint.__value__)
    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        return _union(value, typing.get_args(hint))
    if hint is Any:
        return _object(value) if _tagged(value) else value
    if isinstance(hint, type) and dataclasses.is_dataclass(hint):
        return _object(value, hint)
    if isinstance(hint, type) and issubclass(hint, Enum):
        return hint(value)
    if origin is not None and isinstance(origin, type) and issubclass(origin, Mapping):
        _, item = typing.get_args(hint) or (Any, Any)
        return {k: from_json(v, item) for k, v in value.items()}
    if origin is not None and isinstance(origin, type) and issubclass(origin, Sequence) \
            and not issubclass(origin, str):
        args = typing.get_args(hint)
        return tuple(from_json(v, args[0] if args else Any) for v in value)
    return value


def _tagged(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") in CLASSES


def _union(value: Any, options: Sequence[Any]) -> Any:
    options = [o for o in options if o is not type(None)]
    if _tagged(value):
        for option in options:
            classes = _classes(option)
            if value["type"] in classes:
                return _object(value, classes[value["type"]])
    for option in options:
        if isinstance(option, type) and issubclass(option, Enum) and isinstance(value, str):
            return option(value)
    for option in options:  # a list or mapping of something
        if typing.get_origin(option) is not None:
            return from_json(value, option)
    return value


def _classes(hint: Any) -> dict[str, type]:
    if isinstance(hint, typing.TypeAliasType):
        hint = hint.__value__
    if typing.get_origin(hint) in (typing.Union, types.UnionType):
        found: dict[str, type] = {}
        for arg in typing.get_args(hint):
            found.update(_classes(arg))
        return found
    if isinstance(hint, type) and dataclasses.is_dataclass(hint):
        return {hint.__name__: hint}
    return {}


def _object(value: dict[str, Any], cls: type | None = None) -> Any:
    cls = cls or CLASSES[value["type"]]
    hints = _hints(cls)
    return cls(**{f.name: from_json(value[f.name], hints[f.name])
                  for f in dataclasses.fields(cls) if f.name in value})


@cache
def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls, vars(api))
