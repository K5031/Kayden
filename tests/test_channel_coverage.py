"""The headless channel carries the whole RuntimeAPI: every command is a request the
headless frontend answers and the remote runtime sends. A command added to RuntimeAPI and
forgotten in either breaks sub-sessions and programs that drive Kadence."""

from __future__ import annotations

import inspect

from kadence.api import RuntimeAPI
from kadence.frontends.headless import REQUESTS
from kadence.frontends.remote import RemoteRuntime

NOT_REQUESTS = {"events", "restart"}  # events are notifications; restart is the parent's


def commands() -> set[str]:
    return {name for name, member in vars(RuntimeAPI).items()
            if not name.startswith("_") and inspect.isfunction(member)}


def test_every_command_is_a_request():
    assert set(REQUESTS) == commands() - NOT_REQUESTS


def test_the_remote_runtime_has_every_command_with_the_same_parameters():
    for name in commands():
        remote = getattr(RemoteRuntime, name, None)
        assert remote is not None, f"RemoteRuntime lacks {name}"
        assert (list(inspect.signature(remote).parameters)
                == list(inspect.signature(getattr(RuntimeAPI, name)).parameters)), name
