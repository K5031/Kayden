"""Claude's own web search: offered as a tool Anthropic runs, recorded as ServerToolUse,
replayed to Anthropic, continued when the API pauses a turn, charged per search."""

from __future__ import annotations

import pytest

from kadence.api import (
    InferenceRequest,
    Message,
    Role,
    ServerToolUse,
    Text,
    ToolResult,
    ToolSpec,
    ToolStatus,
)
from tests.conformance.core_suite import WEATHER_TOOL, collect, user
from tests.core.stub import Reply
from tests.core.test_anthropic import (
    AnthropicDriver,
    block,
    finish,
    message_start,
    text_block,
)

OPUS = "claude-opus-5-5"
RESULT = [{"type": "web_search_result", "title": "2026 World Championship",
           "url": "https://en.wikipedia.org/wiki/Worlds_2026", "encrypted_content": "E1",
           "page_age": None}]


def search_blocks(start: int, query: str = "worlds 2026 final") -> list:
    return [
        *block(start, {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
                       "input": {}},
               [{"type": "input_json_delta", "partial_json": json_query(query)}]),
        *block(start + 1, {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1",
                           "content": RESULT}, []),
    ]


def json_query(query: str) -> str:
    return '{"query": "' + query + '"}'


def cited_text(index: int) -> list:
    citation = {"type": "web_search_result_location", "url": RESULT[0]["url"],
                "title": RESULT[0]["title"], "encrypted_index": "I1",
                "cited_text": "Gen.G and BLG"}
    return block(index, {"type": "text", "text": ""},
                 [{"type": "text_delta", "text": "Gen.G meets BLG."},
                  {"type": "citations_delta", "citation": citation}])


def searched(stop: str = "end_turn", searches: int = 1) -> list:
    events = finish(stop)
    events[0][1]["usage"]["server_tool_use"] = {"web_search_requests": searches}
    return events


def request(model: str = OPUS, messages=None) -> InferenceRequest:
    search = ToolSpec("web_search", "search", {"type": "object"})
    return InferenceRequest(model, "", messages or [user("Who plays the final?")],
                            [search, WEATHER_TOOL])


@pytest.fixture
def driver() -> AnthropicDriver:
    return AnthropicDriver(models={OPUS: {"input_usd_per_mtok": 4, "output_usd_per_mtok": 20}})


async def test_web_search_is_offered_as_anthropics_own(driver):
    assert [t.name for t in await driver.core.list_tools()] == ["web_search"]
    driver.stub.queue(Reply(events=[message_start(), *text_block(0, ["ok"]), *finish("end_turn")]))
    await collect(driver.core, request())
    tools = driver.stub.bodies[-1]["tools"]
    assert tools[0] == {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}
    assert tools[1]["name"] == WEATHER_TOOL.name  # Kadence's own tools stay as they are
    driver.stub.queue(Reply(events=[message_start(), *text_block(0, ["ok"]), *finish("end_turn")]))
    await collect(driver.core, request("claude-haiku-5-5"))
    assert driver.stub.bodies[-1]["tools"][0]["type"] == "web_search_20250305"


async def test_a_search_becomes_a_step_with_its_sources(driver):
    driver.stub.queue(Reply(events=[message_start(), *search_blocks(0), *cited_text(2),
                                    *searched()]))
    events = await collect(driver.core, request())
    completed = events[-1]
    [use] = [b for b in completed.message.blocks if isinstance(b, ServerToolUse)]
    assert use.name == "web_search" and use.input == {"query": "worlds 2026 final"}
    assert use.summary == "Searched the web for “worlds 2026 final”" and not use.is_error
    assert "2026 World Championship" in use.content and RESULT[0]["url"] in use.content
    texts = [b.text for b in completed.message.blocks if isinstance(b, Text)]
    assert texts[0] == "Gen.G meets BLG."
    assert texts[-1] == f"\n\nSources: [2026 World Championship]({RESULT[0]['url']})"
    assert completed.usage.extra_cost_usd == pytest.approx(0.01)


async def test_a_paused_turn_is_continued(driver):
    driver.stub.queue(Reply(events=[message_start(), *search_blocks(0), *searched("pause_turn")]))
    driver.stub.queue(Reply(events=[message_start(), *cited_text(0), *searched("end_turn", 0)]))
    events = await collect(driver.core, request())
    assert len(driver.stub.bodies) == 2
    resumed = driver.stub.bodies[1]["messages"][-1]
    assert resumed["role"] == "assistant"
    assert [c["type"] for c in resumed["content"]] == ["server_tool_use",
                                                       "web_search_tool_result"]
    blocks = events[-1].message.blocks
    assert any(isinstance(b, ServerToolUse) for b in blocks)
    assert any(isinstance(b, Text) and b.text == "Gen.G meets BLG." for b in blocks)


async def test_a_search_is_replayed_to_anthropic_and_as_text_to_another_model(driver):
    driver.stub.queue(Reply(events=[message_start(), *search_blocks(0), *cited_text(2),
                                    *searched()]))
    answer = (await collect(driver.core, request()))[-1].message
    later = [user("Who plays the final?"), answer, user("And who is favoured?")]
    # The search went with the turn that made it; a later question gets it as text.
    for model, expected in ((OPUS, ["text", "text", "text"]),
                            ("claude-sonnet-5-5", ["text", "text", "text"])):
        driver.stub.queue(Reply(events=[message_start(), *text_block(0, ["ok"]),
                                        *finish("end_turn")]))
        await collect(driver.core, request(model, later))
        replayed = driver.stub.bodies[-1]["messages"][1]
        assert [c["type"] for c in replayed["content"]] == expected, model
    as_text = driver.stub.bodies[-1]["messages"][1]["content"][0]["text"]
    assert "Searched the web for" in as_text and RESULT[0]["url"] in as_text


async def test_a_failed_search_is_an_error_step(driver):
    error = {"type": "web_search_tool_result_error", "error_code": "too_many_requests"}
    events = [message_start(),
              *block(0, {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
                         "input": {"query": "x"}}, []),
              *block(1, {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1",
                         "content": error}, []),
              *text_block(2, ["Search failed."]), *searched()]
    driver.stub.queue(Reply(events=events))
    [use] = [b for b in (await collect(driver.core, request()))[-1].message.blocks
             if isinstance(b, ServerToolUse)]
    assert use.is_error and "too_many_requests" in use.content


async def test_switched_off_search_falls_back_to_kadences(driver):
    driver.stub.queue(Reply(status=400, json_body={"type": "error", "error": {
        "type": "invalid_request_error",
        "message": "Web search is not enabled for your organization."}}))
    driver.stub.queue(Reply(events=[message_start(), *text_block(0, ["ok"]), *finish("end_turn")]))
    await collect(driver.core, request())
    assert all(t.get("type") != "web_search_20260209" for t in driver.stub.bodies[-1]["tools"])
    assert await driver.core.list_tools() == []


def tool_results() -> Message:
    """The results of the client tools the answer also called: the same turn goes on."""
    return Message("r", Role.TOOL, (ToolResult("toolu_1", ToolStatus.OK, "done"),), 0.0)


def filtered_search() -> list:
    """As Opus 5.5 sends it: code (dynamic filtering) calls the search, and the code's
    result comes after the search's."""
    return [
        *block(0, {"type": "server_tool_use", "id": "srvtoolu_code", "name": "code_execution",
                   "input": {"code": "search()"}}, []),
        *block(1, {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
                   "input": {"query": "worlds 2026 final"},
                   "caller": {"type": "code_execution_20260120", "tool_id": "srvtoolu_code"}},
               []),
        *block(2, {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1",
                   "content": RESULT}, []),
        *block(3, {"type": "code_execution_tool_result", "tool_use_id": "srvtoolu_code",
                   "content": {"type": "code_execution_result", "stdout": "filtered",
                               "stderr": "", "return_code": 0, "content": []}}, []),
        *cited_text(4),
    ]


async def test_filtered_searches_are_replayed_in_the_order_they_came(driver):
    driver.stub.queue(Reply(events=[message_start(), *filtered_search(), *searched()]))
    answer = (await collect(driver.core, request()))[-1].message
    shown = [b for b in answer.blocks if isinstance(b, ServerToolUse)]
    assert [b.name for b in shown] == ["web_search"]  # the filtering code is not a step
    driver.stub.queue(Reply(events=[message_start(), *text_block(0, ["ok"]), *finish("end_turn")]))
    await collect(driver.core, request(OPUS, [user("q"), answer, tool_results()]))
    replayed = driver.stub.bodies[-1]["messages"][1]["content"]
    assert [c.get("id") or c.get("tool_use_id") or c["type"] for c in replayed][:4] == [
        "srvtoolu_code", "srvtoolu_1", "srvtoolu_1", "srvtoolu_code"]


async def test_searches_the_api_will_not_take_back_go_back_as_text(driver):
    driver.stub.queue(Reply(events=[message_start(), *filtered_search(), *searched()]))
    answer = (await collect(driver.core, request()))[-1].message
    driver.stub.queue(Reply(status=400, json_body={"type": "error", "error": {
        "type": "invalid_request_error", "message": "source tool `srvtoolu_x` not found"}}))
    driver.stub.queue(Reply(events=[message_start(), *text_block(0, ["ok"]), *finish("end_turn")]))
    events = await collect(driver.core, request(OPUS, [user("q"), answer, tool_results()]))
    assert events[-1].message.blocks[0].text == "ok"
    retried = driver.stub.bodies[-1]["messages"][1]["content"]
    assert retried[0]["type"] == "text" and "Searched the web for" in retried[0]["text"]
