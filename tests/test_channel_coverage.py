"""The headless channel carries the whole RuntimeAPI: every command is a request the
headless frontend answers and the remote runtime sends. A command added to RuntimeAPI and
forgotten in either breaks sub-sessions and programs that drive Kadence. The setup runtime
(a Kadence that could not start) takes every command too, to refuse it with its reason."""

from __future__ import annotations

import inspect

import pytest

from kadence.api import Refusal, RuntimeAPI, RuntimeStateError
from kadence.frontends.headless import REQUESTS
from kadence.frontends.remote import RemoteRuntime
from kadence.runtime import RuntimeConfig
from kadence.runtime.setup import SetupRuntime

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


def test_the_setup_runtime_has_every_command_with_the_same_parameters():
    for name in commands():
        setup = getattr(SetupRuntime, name, None)
        assert setup is not None, f"SetupRuntime lacks {name}"
        assert (list(inspect.signature(setup).parameters)
                == list(inspect.signature(getattr(RuntimeAPI, name)).parameters)), name


async def test_a_kadence_that_could_not_start_says_so_whatever_is_sent(tmp_path, monkeypatch):
    monkeypatch.delenv("KADENCE_PERMISSION_CEILING", raising=False)
    setup = SetupRuntime(RuntimeConfig(data_dir=tmp_path), "terminal", "no API key")
    for command in (setup.send("look", files=["a.png"], spoken=True), setup.notify("x"),
                    setup.set_plan([]), setup.set_duty(None),
                    setup.resolve_proposal("p", True)):
        with pytest.raises(RuntimeStateError, match="could not start: no API key") as refused:
            await command
        assert refused.value.reason is Refusal.UNAVAILABLE
