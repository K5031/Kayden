"""API values survive the headless channel's JSON both ways."""

from __future__ import annotations

import json

import pytest

from kadence.api import (
    AgentState,
    Budget,
    ConversationInfo,
    Decision,
    Duty,
    DutyKind,
    EventEnvelope,
    Message,
    MessageCommitted,
    ModelInfo,
    OperationInfo,
    OperationResult,
    PermissionPolicy,
    PermissionRequest,
    PermissionRequested,
    PlanItem,
    PlanStatus,
    Reasoning,
    Role,
    RuntimeSnapshot,
    ServerToolUse,
    Settings,
    SettingsUpdate,
    StartOptions,
    StateChanged,
    Text,
    ToolCall,
    ToolResult,
    ToolStatus,
    Usage,
    UsageSnapshot,
)
from kadence.frontends.wire import from_json, to_json

CALL = ToolCall("c1", "search", {"type": "Text", "query": "x"})  # arguments stay a dict
MESSAGE = Message("m1", Role.ASSISTANT, (
    Text("hi"), Reasoning("hmm", state={"sig": "s"}, model="m"), CALL,
    ToolResult("c1", ToolStatus.OK, "found"), OperationResult("c2", ToolStatus.ERROR, "no"),
    ServerToolUse("s1", "web_search", {"q": "x"}, "Searched", "pages")), 1.5)
INFO = ConversationInfo("conv", "Title", 1.0, 2.0, folder="/w",
                        duty=Duty("Watch", DutyKind.GOAL),
                        plan=(PlanItem("Read", PlanStatus.DOING, 3.0, "n"),))


@pytest.mark.parametrize("value", [
    MESSAGE, INFO,
    StateChanged(AgentState.THINKING, 2),
    MessageCommitted(MESSAGE),
    PermissionRequested(PermissionRequest("r1", CALL, read_only=False, reason="why")),
    RuntimeSnapshot(
        seq=4, state=AgentState.IDLE, conversation=INFO, config=None, model="m",
        usage=UsageSnapshot(Usage(1, 2, cost_usd=0.5), 3, 4.0, {"max_cost_usd": 1.0}),
        operations=(OperationInfo(CALL, 1.0, "half", True),), pending_permissions=(),
        queued_messages=(), duty_wake_at=9.0, help_needed="?"),
    SettingsUpdate(budget=Budget(max_cost_usd=2.0), permissions=PermissionPolicy.ASK),
    StartOptions(conversation_id="c"),
    ModelInfo("m", "M", context_window=10),
])
def test_values_round_trip(value):
    assert from_json(json.loads(json.dumps(to_json(value)))) == value


def test_an_envelope_and_enums_by_hint():
    envelope = EventEnvelope(3, StateChanged(AgentState.IDLE))
    assert from_json(to_json(envelope)) == envelope
    assert from_json("allow_once", Decision) is Decision.ALLOW_ONCE


def test_settings_round_trip():
    from tests.fakes.settings import settings

    value: Settings = settings()
    decoded = from_json(json.loads(json.dumps(to_json(value))))
    assert isinstance(decoded, Settings)
    assert to_json(decoded) == to_json(value)  # lists come back as tuples
