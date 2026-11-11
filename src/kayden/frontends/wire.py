"""How the headless channel carries RuntimeAPI. Its commands (COMMANDS) are read from the
RuntimeAPI protocol itself, so both ends follow it: the headless frontend answers a request
by calling the command of that name, and the remote runtime sends one per command.

Values: dataclasses as JSON objects with their class name under "type", enums as their
values, tuples as lists. from_json reverses it, guided by the API's type hints, so a dict
that only looks like an object (a tool call's arguments) stays a dict, and an object sent
without its "type" (a duty, a plan item) still becomes the type the hint names."""

from __future__ import annotations

import dataclasses
import inspect
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
# Every RuntimeAPI command but events(), which the channel carries as notifications.
COMMANDS: frozenset[str] = frozenset(
    name for name, member in vars(api.RuntimeAPI).items()
    if not name.startswith("_") and inspect.isfunction(member)) - {"events"}


def command_args(name: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """A request's parameters as the command's arguments, decoded by its type hints.
    TypeError when they do not fit its signature."""
    command = getattr(api.RuntimeAPI, name)
    inspect.signature(command).bind(None, **params)
    hints = _hints(command)
    return {key: from_json(value, hints.get(key, Any)) for key, value in params.items()}


def result_hint(name: str) -> Any:
    return _hints(getattr(api.RuntimeAPI, name)).get("return", Any)


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
    if isinstance(value, dict) and len(classes := _classes(typing.Union[*options])) == 1:
        return _object(value, next(iter(classes.values())))  # sent without its "type"
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
def _hints(target: Any) -> dict[str, Any]:
    return typing.get_type_hints(target, vars(api))
