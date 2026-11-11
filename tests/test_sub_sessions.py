"""Sub-sessions: the frontend hosts children, headless Kayden processes the agent starts
for self-contained tasks; each reports back and can be watched while it works."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
import tomli_w

from kayden.api import OperationResult, PermissionPolicy, ToolResult, ToolStatus
from kayden.frontends.sessions import SubSessions
from kayden.runtime import InstanceConfig, ModuleSpec, RuntimeConfig, run_instance
from tests.conftest import Harness, eventually
from tests.fakes.core import ScriptedCore, Step, call

ROOT = Path(__file__).resolve().parents[1]


def finish(report: str) -> dict[str, Any]:
    return {"calls": [{"name": "finish_goal", "arguments": {"report": report}}]}


@pytest.fixture
def child_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Children import the test fakes named in the copied config.toml."""
    monkeypatch.setenv("PYTHONPATH", str(ROOT))


async def parent(make: Any, tmp_path: Path, steps: list[Step], child_script: list[Any],
                 child_runtime: dict[str, Any] | None = None) -> tuple[Harness, SubSessions]:
    data = tmp_path / "parent"
    data.mkdir()
    (data / "config.toml").write_text(tomli_w.dumps({
        "core": {"factory": "tests.fakes.core:factory", "options": {"script": child_script}},
        "tools": {"factory": "tests.fakes.tools:factory", "options": {"instant": ["lookup"]}},
        **(child_runtime or {})}))
    sessions = SubSessions(data, check_in_s=(0.05, 0.2))
    h: Harness = await make(core=ScriptedCore(steps), data_dir=data, more_tools=[sessions],
                            permissions=PermissionPolicy.ASK)
    h.auto_approve()
    await h.start(folder=str(tmp_path))
    return h, sessions


def outcome(transcript: list[Any], call_id: str) -> OperationResult | ToolResult | None:
    """The call's final result, whether it finished in the foreground or the background."""
    return next((b for m in transcript for b in m.blocks
                 if isinstance(b, OperationResult | ToolResult) and b.call_id == call_id
                 and b.status != ToolStatus.BACKGROUNDED), None)


async def test_a_child_works_on_its_task_and_reports_back(make, tmp_path, child_env):
    h, sessions = await parent(make, tmp_path, [
        Step(calls=[call("start_session", {"task": "Write a haiku", "budget_usd": 0.5},
                         id="s")]),
        Step(text="A sub-session is on it."),
        Step(text="Here is the haiku.")],
        [{"text": "Drafting."}, finish("Haiku written."), {"text": "Done."}])
    await h.turn("Get me a haiku")
    result = await eventually(lambda: outcome(h.store.conversations[h.conversation.id]
                                              .messages, "s"), timeout=30)
    assert result.status == ToolStatus.OK and "Haiku written." in result.content
    [child] = sessions.sessions
    assert child.task == "Write a haiku" and child.state == "done"
    assert child.conversation_id == h.conversation.id
    assert any("Drafting." in text for _, text in child.lines())
    settings = tomllib.loads((child.data_dir / "settings.toml").read_text())
    assert settings["budget"] == {"max_cost_usd": 0.5}
    assert settings["work_folder"] == str(tmp_path)
    await eventually(lambda: child.returncode is not None, timeout=10)  # it quit


async def test_a_question_comes_back_and_an_answer_lets_it_finish(make, tmp_path, child_env):
    h, sessions = await parent(make, tmp_path, [
        Step(calls=[call("start_session", {"task": "Write a poem"}, id="s")]),
        Step(text="Started."),
        Step(calls=[call("message_session", {"session": "1", "text": "A sonnet."}, id="m")]),
        Step(text="Answered."),
        Step(text="Got the sonnet.")],
        [{"calls": [{"name": "ask_for_help", "arguments": {"question": "Which form?"}}]},
         {"text": "Waiting."}, finish("Wrote a sonnet."), {"text": "Done."}])
    await h.turn("Get me a poem")
    asked = await eventually(lambda: outcome(h.store.conversations[h.conversation.id]
                                             .messages, "s"), timeout=30)
    assert "Which form?" in asked.content and "message_session" in asked.content
    answered = await eventually(lambda: outcome(h.store.conversations[h.conversation.id]
                                                .messages, "m"), timeout=30)
    assert "Wrote a sonnet." in answered.content
    assert sessions.sessions[0].state == "done"


async def test_cancelling_the_call_stops_the_child(make, tmp_path, child_env):
    h, sessions = await parent(make, tmp_path, [
        Step(calls=[call("start_session", {"task": "Think forever"}, id="s")]),
        Step(text="Started.")],
        [{"hang": True}])
    await h.turn("Go")
    await eventually(lambda: sessions.sessions and sessions.sessions[0].state == "working",
                     timeout=30)
    child = sessions.sessions[0]
    await h.runtime.cancel_operation("s")
    await eventually(lambda: child.returncode is not None, timeout=15)
    assert child.state == "stopped"


async def test_a_childs_permission_prompt_is_refused_and_reported(make, tmp_path, child_env):
    h, sessions = await parent(make, tmp_path, [
        Step(calls=[call("start_session", {"task": "Look it up"}, id="s")]),
        Step(text="Started."), Step(text="Reported.")],
        [{"calls": [{"name": "lookup"}]}, finish("Could not look it up."), {"text": "Done."}],
        child_runtime={"runtime": {"permissions": "ask"}})
    await h.turn("Go")
    result = await eventually(lambda: outcome(h.store.conversations[h.conversation.id]
                                              .messages, "s"), timeout=30)
    assert "refused" in result.content and "lookup" in result.content
    assert any("refused" in text for _, text in sessions.sessions[0].lines())


async def test_unknown_sessions_are_errors(make, tmp_path, child_env):
    h, _ = await parent(make, tmp_path, [
        Step(calls=[call("message_session", {"session": "9", "text": "hi"}, id="m")]),
        Step(text="ok")], [])
    await h.turn("Go")
    assert (await h.results())["m"].status == ToolStatus.ERROR


async def test_a_frontend_offers_its_tools(tmp_path, clean_env):
    from tests.fakes.tools import FakeTools

    extra = FakeTools()
    extra.instant("from_the_frontend")
    seen: list[str] = []

    async def script(runtime, frontend) -> None:
        seen.extend(t.name for t in await runtime.list_tools())

    import tests.fakes.frontend as fakes

    original = fakes.factory

    def factory(options, paths):
        return original({**options, "script": script, "tools": [extra]}, paths)

    fakes.tools_factory = factory  # type: ignore[attr-defined]
    try:
        await run_instance(InstanceConfig(
            runtime=RuntimeConfig(data_dir=tmp_path / "d"),
            core=ModuleSpec("tests.fakes.core:factory"),
            memory=ModuleSpec("tests.fakes.memory:factory"),
            tools=ModuleSpec("tests.fakes.tools:factory", {"instant": ["lookup"]}),
            frontend=ModuleSpec("tests.fakes.frontend:tools_factory")))
    finally:
        del fakes.tools_factory  # type: ignore[attr-defined]
    assert seen.index("from_the_frontend") > seen.index("lookup")
    assert extra.closed
