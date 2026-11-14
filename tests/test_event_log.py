"""The event log records what the runtime did, never what was said or run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kadence.api import AgentState, Decision, SettingsUpdate
from tests.conftest import Harness, eventually
from tests.fakes.core import ScriptedCore, Step, call
from tests.fakes.tools import FakeTools


def records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def of(lines: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [r["data"] for r in lines if r.get("event") == event]


async def test_the_log_holds_no_content(make: Any) -> None:
    tools = FakeTools()
    tools.instant("t", "SECRET-OUTPUT", read_only=False)
    core = ScriptedCore([
        Step(text="SECRET-THOUGHT", calls=[call("t", {"x": "SECRET-ARG"}, id="c1")]),
        Step(text="SECRET-REPLY")])
    h: Harness = await make(core=core, tools=tools)
    conversation = await h.start()
    await h.runtime.update_settings(SettingsUpdate(system_prompt="SECRET-PROMPT"))
    await h.runtime.rename_conversation(conversation.id, "SECRET-TITLE")
    await h.runtime.send("SECRET-QUESTION")
    await eventually(lambda: h.rec.state == AgentState.AWAITING_PERMISSION)
    request = h.runtime.snapshot().pending_permissions[0]
    await h.runtime.resolve_permission(request.id, Decision.ALLOW_ONCE)
    await eventually(lambda: len(core.requests) == 2 and h.rec.state == AgentState.IDLE)
    await h.close()

    path = h.data_dir / "events.jsonl"
    assert "SECRET" not in path.read_text()
    lines = records(path)
    committed = of(lines, "MessageCommitted")
    assert [(m["role"], m["blocks"]) for m in committed] == [
        ("user", ["text"]), ("assistant", ["text", "tool_call"]), ("tool", ["tool_result"]),
        ("assistant", ["text"])]
    assert all(m["message_id"] for m in committed)
    assert of(lines, "ToolStarted") == [{"call_id": "c1", "name": "t"}]
    (finished,) = of(lines, "ToolFinished")
    assert finished["call_id"] == "c1" and finished["status"] == "ok"
    assert of(lines, "PermissionRequested") == [
        {"request_id": request.id, "call_id": "c1", "name": "t", "read_only": False,
         "confined": False}]
    (changed, *_) = of(lines, "SettingsChanged")
    assert changed["settings"]["system_prompt_chars"] == len("SECRET-PROMPT")
    assert "system_prompt" not in changed["settings"]
    assert all("title" not in u["info"] and "preview" not in u["info"]
               for u in of(lines, "ConversationUpdated"))

