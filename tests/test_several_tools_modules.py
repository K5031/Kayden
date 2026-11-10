"""Several Tools modules run at once: each call goes to the module that offered its name."""

from __future__ import annotations

from typing import Any

from kayden.api import PermissionPolicy, StartOptions, ToolStatus
from tests.conftest import Harness
from tests.fakes.core import ScriptedCore, Step, call
from tests.fakes.tools import FakeTools


async def two(make: Any, *steps: Step, **config: Any) -> tuple[Harness, FakeTools, FakeTools]:
    first, second = FakeTools(), FakeTools()
    first.instant("files", "from first")
    first.instant("both", "first wins")
    second.instant("github", "from second", read_only=False)
    second.instant("both", "second loses")
    h = await make(core=ScriptedCore([*steps, Step(text="done")]), tools=first,
                   more_tools=[second], **config)
    return h, first, second


async def test_each_call_goes_to_the_module_that_offered_it(make: Any) -> None:
    h, first, second = await two(make, Step(calls=[
        call("files", id="f"), call("github", id="g"), call("both", id="b")]),
        permissions=PermissionPolicy.ASK)
    h.auto_approve()
    await h.start()
    await h.turn("go")
    results = await h.results()
    assert results["f"].content == "from first" and results["g"].content == "from second"
    assert results["b"].content == "first wins"  # the later module's copy is dropped
    assert first.started == ["f", "b"] and second.started == ["g"]
    names = [spec.name for spec in await h.runtime.list_tools()]
    assert names.count("both") == 1


async def test_the_owner_judges_confinement(make: Any) -> None:
    h, first, second = await two(make, Step(calls=[call("github", id="g")]),
                                 permissions=PermissionPolicy.ALLOW_WORKSPACE)
    second.confined_names = {"github"}
    first.confined_names = set()
    await h.start()
    await h.turn("go")
    assert (await h.results())["g"].status == ToolStatus.OK  # no prompt: its owner said so
    assert all(c.name != "github" for c in first.received)


async def test_folders_go_to_every_module_that_takes_one(make: Any, tmp_path: Any) -> None:
    h, first, _second = await two(make)
    first.folders = []  # second keeps one fixed folder
    await h.runtime.start(StartOptions(folder=str(tmp_path)))
    assert first.folders == [tmp_path.resolve()]


async def test_every_module_is_closed(make: Any) -> None:
    h, first, second = await two(make)
    await h.start()
    await h.close()
    assert first.closed and second.closed
