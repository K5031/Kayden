"""The model sees when each user message was sent; the transcript keeps the text as typed."""

from __future__ import annotations

from datetime import datetime

from kayden.api import Role, Text
from tests.conftest import Harness
from tests.fakes.core import Step, call
from tests.fakes.tools import FakeTools


def user_texts(request) -> list[str]:
    return [b.text for m in request.messages if m.role == Role.USER
            for b in m.blocks if isinstance(b, Text)]


async def test_user_messages_carry_the_time_they_were_sent(make):
    h: Harness = await make()
    await h.start()
    await h.turn("hello")
    [sent] = [m for m in await h.transcript() if m.role == Role.USER]
    stamp = datetime.fromtimestamp(sent.created_at).astimezone().strftime("%a %Y-%m-%d %H:%M %z")
    assert user_texts(h.core.requests[0]) == [f"[Sent {stamp[:-2]}:{stamp[-2:]}] hello"]
    assert sent.blocks == (Text("hello"),)  # stored as typed


async def test_the_stamp_does_not_change_between_requests(make):
    tools = FakeTools()
    tools.instant("look")
    h: Harness = await make(tools=tools)
    h.core.add(Step(calls=[call("look")]), Step(text="done"))
    await h.start()
    await h.turn("look it up")
    first, second = (user_texts(r)[0] for r in h.core.requests[:2])
    assert first == second and first.endswith("] look it up")


async def test_tool_results_are_not_stamped(make):
    tools = FakeTools()
    tools.instant("look", "found")
    h: Harness = await make(tools=tools)
    h.core.add(Step(calls=[call("look")]), Step(text="done"))
    await h.start()
    await h.turn("go")
    tool = [m for m in h.core.requests[1].messages if m.role == Role.TOOL]
    assert tool and all(not getattr(b, "content", "").startswith("[Sent")
                        for m in tool for b in m.blocks)
