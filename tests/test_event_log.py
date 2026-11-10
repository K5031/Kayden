"""The event log records what the runtime did, never what was said or run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kayden.api import AgentState, Decision, SettingsUpdate
from kayden.runtime.events import LOG_FORMAT
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
    assert lines[0] == {"log_format": LOG_FORMAT}
    committed = of(lines, "MessageCommitted")
    assert [(m["role"], m["blocks"]) for m in committed] == [
        ("user", ["text"]), ("assistant", ["text", "tool_call"]), ("tool", ["tool_result"]),
        ("assistant", ["text"])]
    assert all(m["message_id"] for m in committed)
    assert of(lines, "ToolStarted") == [{"call_id": "c1", "name": "t"}]
    (finished,) = of(lines, "ToolFinished")
    assert finished["call_id"] == "c1" and finished["status"] == "ok"
    assert of(lines, "PermissionRequested") == [
        {"request_id": request.id, "call_id": "c1", "name": "t", "read_only": False}]
    (changed, *_) = of(lines, "SettingsChanged")
    assert changed["settings"]["system_prompt_chars"] == len("SECRET-PROMPT")
    assert "system_prompt" not in changed["settings"]
    assert all("title" not in u["info"] and "preview" not in u["info"]
               for u in of(lines, "ConversationUpdated"))


OLD_RECORDS = [
    {"ts": 1.0, "seq": 1, "event": "MessageCommitted", "data": {
        "message": {"id": "m1", "role": "user", "created_at": 1.0,
                    "blocks": [{"text": "OLD-SECRET"}]}, "stop": None}},
    {"ts": 2.0, "seq": 2, "event": "ToolStarted", "data": {
        "call": {"id": "c1", "name": "shell", "arguments": {"command": "OLD-SECRET"},
                 "raw_arguments": None}}},
    {"ts": 3.0, "seq": 3, "event": "ToolFinished", "data": {
        "result": {"call_id": "c1", "status": "ok", "content": "OLD-SECRET"},
        "duration_s": 0.5}},
    {"ts": 4.0, "seq": 4, "event": "MessageQueued", "data": {
        "message_id": "q1", "text": "OLD-SECRET"}},
    {"ts": 5.0, "seq": 5, "event": "SettingsChanged", "data": {
        "settings": {"model": "m", "system_prompt": "OLD-SECRET"}}},
    {"ts": 6.0, "seq": 6, "event": "ConversationUpdated", "data": {
        "info": {"id": "conv", "title": "OLD-SECRET", "preview": "OLD-SECRET",
                 "archived": False}}},
    {"ts": 7.0, "seq": 7, "event": "PermissionRequested", "data": {
        "request": {"id": "p1", "read_only": True, "call": {
            "id": "c1", "name": "shell", "arguments": {"command": "OLD-SECRET"}}}}},
    {"ts": 8.0, "decision": "budget_check", "before": "inference", "ok": True, "reason": None},
]


async def test_existing_logs_are_stripped_once(make: Any, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    old = "".join(json.dumps(r) + "\n" for r in OLD_RECORDS) + "not json OLD-SECRET\n"
    for name in ("events.jsonl", "events-session.jsonl", "events-continuous.jsonl"):
        (data_dir / name).write_text(old)
    h: Harness = await make(data_dir=data_dir)
    await h.start()
    await h.close()

    for name in ("events.jsonl", "events-session.jsonl", "events-continuous.jsonl"):
        assert "OLD-SECRET" not in (data_dir / name).read_text()
        lines = records(data_dir / name)
        assert lines[0] == {"log_format": LOG_FORMAT}
        assert lines[1] == {"ts": 1.0, "seq": 1, "event": "MessageCommitted", "data": {
            "message_id": "m1", "role": "user", "stop": None}}
        assert lines[2]["data"] == {"call_id": "c1", "name": "shell"}
        assert lines[3]["data"] == {"call_id": "c1", "status": "ok", "duration_s": 0.5}
        assert lines[4]["data"] == {"message_id": "q1", "chars": len("OLD-SECRET")}
        assert lines[5]["data"] == {"settings": {
            "model": "m", "system_prompt_chars": len("OLD-SECRET")}}
        assert lines[6]["data"] == {"info": {"id": "conv", "archived": False}}
        assert lines[7]["data"] == {"request_id": "p1", "call_id": "c1", "name": "shell",
                                    "read_only": True}
        assert lines[8] == OLD_RECORDS[7]
    # The legacy logs are stripped once and then left alone.
    legacy = data_dir / "events-session.jsonl"
    before = legacy.stat().st_mtime_ns
    h2: Harness = await make(data_dir=data_dir)
    await h2.start()
    await h2.close()
    assert legacy.stat().st_mtime_ns == before
    assert sum(r == {"log_format": LOG_FORMAT} for r in records(data_dir / "events.jsonl")) == 1
