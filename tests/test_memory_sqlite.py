import asyncio
import json
import re
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from kayden.api import (
    PROFILE,
    ContextRequest,
    InstancePaths,
    Memory,
    Message,
    OpaqueBlock,
    Reasoning,
    Role,
    Text,
    ToolCall,
    ToolResult,
    ToolStatus,
)
from kayden.memory.sqlite import SCHEMA_VERSION, CompactionConfig, SqliteMemory, factory
from kayden.runtime.budget import estimate_tokens
from tests.conformance.memory_suite import (
    TOKEN_RE,
    WINDOW,
    FakeCompletion,
    MemoryConformance,
    MemoryFactory,
    active,
    addendum,
    assert_archived,
    assert_well_formed,
    context,
    count_tokens,
    text_message,
    tokens,
    tool_exchange,
)

CONFIG = CompactionConfig(trigger_fraction=0.75, remove_fraction=0.5, summary_fraction=0.05,
                          long_term_fraction=0.05)
SUMMARY_TOKENS = CONFIG.summary_fraction * WINDOW
_FACT_RE = re.compile(r"remember: ([a-z ]+)")


def tagged(kept: list[str], text: str) -> str:
    """A summary for compaction; for the pass after an answer, each "remember: …" fact not
    yet in the profile, appended to it."""
    if not text.startswith("You keep the long-term memory"):
        return f"<summary>\nSummary: {' '.join(kept)}\n</summary>"
    profile = text.split("The profile:")[1].split("Notes shown in full:")[0]
    exchange = text.split("Other notes (title: description):")[1]
    facts = [f for f in dict.fromkeys(_FACT_RE.findall(exchange)) if f not in profile]
    return "".join(f'<append title="{PROFILE}">{f}</append>' for f in facts)


def answer(token: str = "done") -> Message:
    return text_message(token, Role.ASSISTANT)


class TestSqliteMemory(MemoryConformance):
    @pytest.fixture
    def memory_factory(self, tmp_path: Path):
        return lambda: SqliteMemory(tmp_path / "memory.db", CONFIG)

    @pytest.fixture
    def completion(self) -> FakeCompletion:
        return FakeCompletion(render=tagged)

    # The built-in Memory's own: a fixed profile and topic notes.

    async def test_the_profile_is_seen_in_full_other_notes_by_title(
            self, open_memory: MemoryFactory):
        memory = open_memory()
        await self.editable(memory)
        [profile] = await memory.notes()
        assert profile.title == PROFILE and profile.text == ""
        conv = await memory.create_conversation(title="test")
        await memory.append(conv.id, [text_message("tok0001")])
        await memory.save_note(PROFILE, "", "Lives in Oslo.")
        note = await memory.save_note("Kayden", "The user's AI agent project",
                                      "Written in Python; the owner pushes to main.")
        assert note.source_conversation_id is None
        text = await addendum(memory, conv.id)
        assert "Lives in Oslo." in text and "Kayden" in text
        assert "The user's AI agent project" in text and "pushes to main" not in text
        await memory.aclose()

        memory = open_memory()
        renamed = await memory.save_note("Kayden app", "", "Python.", note_id=note.id)
        assert (renamed.id, renamed.description) == (note.id, "The user's AI agent project")
        same = await memory.save_note("kayden APP", "", "Python 3.13.")  # case ignored
        assert same.id == note.id and same.text == "Python 3.13."
        assert [n.title for n in await memory.notes()] == [PROFILE, "Kayden app"]
        with pytest.raises(ValueError):
            await memory.save_note("Other", "", "x", note_id=profile.id)  # keeps its title
        with pytest.raises(ValueError):
            await memory.delete_note(profile.id)
        await memory.delete_note(note.id)
        assert [n.title for n in await memory.notes()] == [PROFILE]
        assert "Kayden" not in await addendum(memory, conv.id)
        with pytest.raises(KeyError):
            await memory.delete_note(note.id)

    async def test_memories_outlive_the_conversation_they_came_from(self, memory: Memory):
        doomed = await memory.create_conversation()
        try:
            await memory.save_note("Diet", "What the user eats", "Is vegetarian",
                                   conversation_id=doomed.id)
            await memory.delete_conversation(doomed.id)
        except NotImplementedError:
            pytest.skip("editing memories and deleting conversations are optional")
        kept = [n for n in await memory.notes() if n.title == "Diet"]
        assert [(n.text, n.source_conversation_id) for n in kept] == [("Is vegetarian", None)]
        with pytest.raises(KeyError):
            await memory.save_note("x", "", "y", conversation_id="missing")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "memory.db"


@pytest.fixture
async def open_store(db: Path):
    opened: list[SqliteMemory] = []

    def open_(config: CompactionConfig = CONFIG) -> SqliteMemory:
        opened.append(SqliteMemory(db, config))
        return opened[-1]

    yield open_
    for memory in opened:
        await memory.aclose()


async def fill(memory: SqliteMemory, conversation_id: str, toks: list[str]) -> list[Message]:
    messages = [text_message(t) for t in toks]
    await memory.append(conversation_id, messages)
    return messages


async def maintain(memory: SqliteMemory, conversation_id: str, completion,
                   window: int = WINDOW) -> bool:
    return await memory.maintain(conversation_id, completion, context(conversation_id, window))


async def test_factory_uses_the_instance_data_dir(tmp_path: Path):
    memory = factory({"trigger_fraction": 0.5}, InstancePaths(tmp_path))
    try:
        assert isinstance(memory, SqliteMemory)
        await memory.create_conversation(title="test")
        assert (tmp_path / "memory.db").exists()
    finally:
        await memory.aclose()
    with pytest.raises(TypeError):
        factory({"no_such_option": 1}, InstancePaths(tmp_path))


@pytest.mark.parametrize("fraction", [0.25, 0.5, 0.75])
async def test_remove_fraction_controls_how_much_is_compacted(open_store, fraction: float):
    memory = open_store(CompactionConfig(remove_fraction=fraction))
    conv = await memory.create_conversation(title="test")
    messages = await fill(memory, conv.id, tokens(0, 40))  # equal-sized messages
    assert await maintain(memory, conv.id, FakeCompletion(render=tagged))
    removed = len(messages) - len(await active(memory, conv.id))
    assert removed == round(len(messages) * fraction)


async def test_trigger_is_a_fraction_of_the_window(open_store):
    conv_messages = [text_message(t) for t in tokens(0, 40)]
    window = 2 * count_tokens(conv_messages)
    for fraction, compacts in ((0.75, False), (0.25, True)):
        memory = open_store(CompactionConfig(trigger_fraction=fraction))
        conv = await memory.create_conversation(title="test")
        await memory.append(conv.id, conv_messages)
        completion = FakeCompletion(render=tagged)
        assert await maintain(memory, conv.id, completion, window) is compacts
        assert bool(completion.calls) is compacts


async def test_compaction_starts_at_ninety_percent_by_default(open_store):
    """The context is used nearly to the full; a margin covers token-count estimates."""
    conv_messages = [text_message(t) for t in tokens(0, 40)]
    size = count_tokens(conv_messages)
    for window, compacts in ((int(size / 0.88), False), (int(size / 0.92), True)):
        memory = open_store(CompactionConfig())
        conv = await memory.create_conversation(title="test")
        await memory.append(conv.id, conv_messages)
        assert await maintain(memory, conv.id, FakeCompletion(render=tagged), window) is compacts


def with_topics(*topics: str) -> Callable[[list[str], str], str]:
    def render(kept: list[str], text: str) -> str:
        listed = "\n".join(f"- {t}" for t in topics)
        return f"<summary>\nSummary: {' '.join(kept)}\n</summary>\n<topics>\n{listed}\n</topics>"
    return render


async def test_the_summary_ends_with_the_topics_covered(open_store):
    """One line per topic survives every rewrite, so the agent knows what to search for."""
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    await fill(memory, conv.id, tokens(0, 40))
    assert await maintain(memory, conv.id, FakeCompletion(
        render=with_topics("Backup script (Nov 3)")))
    summary = await addendum(memory, conv.id)
    assert "Topics so far" in summary and "- Backup script (Nov 3)" in summary
    assert summary.index("Summary:") < summary.index("Topics so far")

    await fill(memory, conv.id, tokens(40, 80))
    completion = FakeCompletion(render=with_topics("Backup script (Nov 3)",
                                                   "Hosting (Nov 4)"))
    assert await maintain(memory, conv.id, completion)
    prompt = completion.calls[0]["system"]
    assert "every topic" in prompt and "- Backup script (Nov 3)" in prompt
    assert "- Hosting (Nov 4)" in await addendum(memory, conv.id)


async def test_an_overlong_summary_loses_prose_before_topics(open_store):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    await fill(memory, conv.id, tokens(0, 40))
    words = " ".join(f"word{i}" for i in range(int(SUMMARY_TOKENS) * 2))

    def render(kept: list[str], text: str) -> str:
        return f"<summary>{words}</summary><topics>\n- Postgres backup script\n</topics>"

    assert await maintain(memory, conv.id, FakeCompletion(render=render))
    state = await memory.inspect(conv.id)
    assert state is not None and state.summary.endswith("- Postgres backup script")
    assert count_tokens(state.summary) <= SUMMARY_TOKENS


async def test_a_long_task_from_one_message_is_compacted_between_its_steps(open_store):
    """One message, then many tool steps: compaction cuts between finished steps, and the
    message the task started from stays in view so the agent keeps its wording."""
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    task = text_message("tok9999")
    steps = [m for t in tokens(0, 20) for m in tool_exchange(t)]
    await memory.append(conv.id, [task, *steps])
    assert await maintain(memory, conv.id, FakeCompletion(render=tagged))
    view = await active(memory, conv.id)
    assert view[0] == task and view[1].role is Role.ASSISTANT
    assert 0 < len(view) - 1 < len(steps) and view[1:] == steps[len(steps) - len(view) + 1:]
    assert_well_formed(view)
    assert await maintain(memory, conv.id, FakeCompletion(render=tagged))  # and again
    again = await active(memory, conv.id)
    assert again[0] == task and len(again) < len(view) and again[1].role is Role.ASSISTANT


async def test_turns_are_still_cut_at_a_message_when_one_is_far_enough_back(open_store):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    first = [text_message("tok9998"), *(m for t in tokens(0, 12) for m in tool_exchange(t))]
    second = [text_message("tok9999"), *(m for t in tokens(12, 4) for m in tool_exchange(t))]
    await memory.append(conv.id, [*first, *second])
    assert await maintain(memory, conv.id, FakeCompletion(render=tagged))
    assert (await active(memory, conv.id))[0] == second[0]


async def test_compaction_uses_light_model_and_summary_budget(open_store):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    await fill(memory, conv.id, tokens(0, 40))
    completion = FakeCompletion(render=tagged)
    await maintain(memory, conv.id, completion)
    [call] = completion.calls
    assert call["light"] is True
    assert call["max_tokens"] >= SUMMARY_TOKENS
    assert "tok0000" in call["messages"][0].blocks[0].text
    assert "tok0039" not in call["messages"][0].blocks[0].text


@pytest.mark.parametrize("output", [
    "", "   \n", "<summary>  </summary>\n<long_term>\n- likes tea\n</long_term>"])
async def test_an_empty_summary_compacts_nothing(open_store, output: str):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    messages = await fill(memory, conv.id, tokens(0, 40))
    assert not await maintain(memory, conv.id, FakeCompletion(render=lambda kept, text: output))
    view = await memory.assemble(context(conv.id, 10_000_000))
    assert [m.id for m in view.messages] == [m.id for m in messages]
    assert view.system_addendum == ""
    assert await maintain(memory, conv.id, FakeCompletion(render=tagged))  # a later pass works


async def test_compaction_of_one_huge_message_is_truncated_to_fit(open_store):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    huge = Message("huge", Role.USER, (Text("tok0001 " + "word " * 20_000),), time.time())
    await memory.append(conv.id, [huge, *[text_message(t) for t in tokens(2, 3)]])
    completion = FakeCompletion(render=tagged, input_tokens=2_000)
    assert await maintain(memory, conv.id, completion)
    [call] = completion.calls
    sent = count_tokens(call["system"]) + count_tokens(call["messages"])
    assert sent + call["max_tokens"] <= 2_000
    assert (await memory.history(conv.id))[0] == huge


async def test_summary_is_held_to_its_budget(open_store):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    await fill(memory, conv.id, tokens(0, 40))
    await maintain(memory, conv.id, FakeCompletion(render=lambda k, t: "word " * 10_000))
    view = await memory.inspect(conv.id)
    assert 0 < count_tokens(view.summary) <= SUMMARY_TOKENS
    assert view.long_term == [""]  # an empty profile, no notes


async def test_what_an_exchange_teaches_is_saved_for_every_conversation(open_store):
    memory = open_store()
    first = await memory.create_conversation(title="test")
    second = await memory.create_conversation(title="test")
    fact = Message("fact", Role.USER, (Text("remember: user prefers green tea."),), time.time())
    await memory.append(first.id, [fact, answer()])
    completion = FakeCompletion(render=tagged)
    assert await maintain(memory, first.id, completion)  # after the answer

    [profile] = await memory.notes()
    assert profile.text == "user prefers green tea" and profile.source_conversation_id == first.id
    assert "user prefers green tea" in await addendum(memory, second.id)

    # Said again elsewhere, it is not saved twice; the profile survives a reopen.
    again = Message("fact2", Role.USER, fact.blocks, time.time())
    await memory.append(second.id, [again, answer()])
    assert not await maintain(memory, second.id, completion)
    await memory.aclose()
    reopened = open_store()
    assert (await reopened.inspect(second.id)).long_term == ["user prefers green tea"]


async def test_the_pass_reads_each_exchange_once_and_never_mid_turn(open_store):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    completion = FakeCompletion(render=tagged)
    await memory.append(conv.id, [text_message("remember: likes jazz")])
    await maintain(memory, conv.id, completion)  # the user's message: no answer yet
    await memory.append(conv.id, tool_exchange("tok0001")[:1])  # an answer that calls a tool
    await maintain(memory, conv.id, completion)
    assert completion.calls == []
    await memory.append(conv.id, [*tool_exchange("tok0001")[1:], answer()])
    assert await maintain(memory, conv.id, completion)
    assert len(completion.calls) == 1 and "likes jazz" in repr(completion.calls[0]["messages"])
    assert not await maintain(memory, conv.id, completion)  # nothing new since
    assert len(completion.calls) == 1


async def test_the_pass_appends_to_notes_it_is_shown_by_title_only(open_store):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    await memory.save_note("Big", "A long note", "y " * 20_000)  # past what the pass is shown
    await memory.save_note("Small", "A short note", "fits")
    completion = FakeCompletion(input_tokens=100_000, render=lambda kept, text: (
        '<append title="Big">one more line</append>'
        '<save title="Trip" description="The Oslo trip">Leaves on Friday.</save>'))
    await memory.append(conv.id, [text_message("tok0001"), answer()])
    assert await maintain(memory, conv.id, completion)
    shown = completion.calls[0]["system"]
    assert "fits" in shown and "y y y" not in shown and "- Big: A long note" in shown
    notes = {n.title: n for n in await memory.notes()}
    assert notes["Big"].text.endswith("y\none more line")
    assert notes["Big"].description == "A long note"  # appending keeps it
    assert (notes["Trip"].description, notes["Trip"].text) == ("The Oslo trip", "Leaves on Friday.")


async def test_a_failed_pass_reads_the_exchange_after_the_next_answer(open_store):
    from kayden.api import CoreError

    class Failing(FakeCompletion):
        async def __call__(self, system, messages, *, max_tokens, light=False,
                       compacting=False):
            raise CoreError("overloaded")

    memory = open_store()
    conv = await memory.create_conversation(title="test")
    await memory.append(conv.id, [text_message("remember: likes jazz"), answer()])
    assert not await maintain(memory, conv.id, Failing())
    await memory.append(conv.id, [text_message("remember: lives in oslo"), answer()])
    completion = FakeCompletion(render=tagged)
    assert await maintain(memory, conv.id, completion)
    assert (await memory.notes())[0].text == "likes jazz\nlives in oslo"


async def test_long_term_rendering_is_a_fraction_of_the_budget(open_store, db: Path):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    await memory.append(conv.id, [text_message("tok0001")])
    for i in range(100):
        await memory.save_note(f"note number {i}", "x" * 100, "the text is never shown")
    budget = 4_000
    rendered = (await memory.assemble(context(conv.id, budget))).system_addendum
    assert "note number 99" in rendered and "note number 0:" not in rendered  # newest first
    assert "the text is never shown" not in rendered
    assert count_tokens(rendered) <= CONFIG.long_term_fraction * budget + 10
    assert len((await memory.inspect(conv.id)).long_term) == 101  # the profile and 100


async def test_untagged_output_becomes_the_summary(open_store):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    await fill(memory, conv.id, tokens(0, 40))
    await maintain(memory, conv.id, FakeCompletion(render=lambda k, t: "  plain summary  "))
    assert (await memory.inspect(conv.id)).summary == "plain summary"


async def test_compaction_never_strands_a_pending_tool_call(open_store):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    before = await fill(memory, conv.id, tokens(0, 5))
    call = Message("call", Role.ASSISTANT, (ToolCall("c1", "slow_job", {}),), time.time())
    await memory.append(conv.id, [call])
    after = await fill(memory, conv.id, tokens(5, 40))
    await maintain(memory, conv.id, FakeCompletion(render=tagged))
    # The cut must precede the call and land on a USER message.
    assert await active(memory, conv.id) == [before[-1], call, *after]

    result = Message("res", Role.TOOL, (ToolResult("c1", ToolStatus.OK, "done"),), time.time())
    await memory.append(conv.id, [result])
    await maintain(memory, conv.id, FakeCompletion(render=tagged))
    tail = await active(memory, conv.id)
    assert (call in tail) == (result in tail)
    await assert_archived(memory, conv.id, before)


async def test_assemble_trims_whole_tool_exchanges(open_store):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    ask = Message("ask", Role.USER, (Text("read it"),), time.time())
    call = Message("call", Role.ASSISTANT, (ToolCall("c1", "read", {}),), time.time())
    result = Message(
        "res", Role.TOOL, (ToolResult("c1", ToolStatus.OK, "x" * 400),), time.time()
    )
    thanks = Message("thanks", Role.USER, (Text("thanks"),), time.time())
    final = Message("final", Role.ASSISTANT, (Text("done"),), time.time())
    await memory.append(conv.id, [ask, call, result, thanks, final])
    budget = count_tokens([result, thanks, final])
    ctx = await memory.assemble(context(conv.id, budget))
    assert list(ctx.messages) == [thanks, final]


async def test_conflicting_append_is_atomic(open_store):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    first = text_message("tok0001")
    await memory.append(conv.id, [first])
    changed = Message(first.id, Role.USER, (Text("tok0009"),), first.created_at)
    with pytest.raises(ValueError):
        await memory.append(conv.id, [text_message("tok0002"), changed])
    assert await memory.history(conv.id) == [first]


async def test_archive_is_append_only(open_store, db: Path):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    await fill(memory, conv.id, tokens(0, 2))
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE messages SET blocks = '[]'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM messages")


async def test_unsupported_schema_version_is_refused(open_store, db: Path):
    memory = open_store()
    await memory.create_conversation(title="test")
    await memory.aclose()
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="schema version 99"):
        await open_store().list_conversations()


def _raw_blocks(db: Path) -> list[list[dict]]:
    with sqlite3.connect(db) as conn:
        return [json.loads(r[0]) for r in conn.execute("SELECT blocks FROM messages ORDER BY seq")]


async def test_stored_tags_are_stable_names(open_store, db: Path):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    await memory.append(conv.id, [Message("m", Role.ASSISTANT, (
        Text("a"), Reasoning("r"), ToolCall("c", "t", {})), 1.0)])
    assert [b["type"] for b in _raw_blocks(db)[0]] == ["text", "reasoning", "tool_call"]


async def test_known_tag_with_unknown_fields_stays_opaque(open_store, db: Path):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    future = OpaqueBlock("text", {"text": "hi", "lang": "en"})
    await memory.append(conv.id, [Message("m", Role.USER, (future,), 1.0)])
    assert (await memory.history(conv.id))[0].blocks == (future,)
    assert _raw_blocks(db)[0] == [{"type": "text", "data": {"text": "hi", "lang": "en"}}]


async def test_history_returns_the_full_transcript(open_store):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    messages = await fill(memory, conv.id, tokens(0, 40))
    await maintain(memory, conv.id, FakeCompletion(render=tagged))
    assert len(await active(memory, conv.id)) < len(messages)
    assert await memory.history(conv.id) == messages


async def test_history_before_unknown_message_raises(open_store):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    await fill(memory, conv.id, tokens(0, 2))
    with pytest.raises(KeyError):
        await memory.history(conv.id, before="nope")


_CRASH_SCRIPT = """
import asyncio, os, sys
from kayden.api import ContextRequest
from kayden.memory.sqlite import SqliteMemory

class Complete:
    def __init__(self, memory):
        self.memory = memory

    def max_input_tokens(self, *, light=False):
        return 10**9

    async def __call__(self, system, messages, *, max_tokens, light=False,
                       compacting=False):
        def arm(conn):
            # Die as the compaction transaction starts to commit, after its writes ran.
            conn.set_trace_callback(lambda sql: sql == "COMMIT" and os._exit(17))
        await self.memory._run(arm)
        return "<summary>never committed</summary>"

async def main(path, conversation_id):
    memory = SqliteMemory(path)
    count = lambda v: len(v) // 4 if isinstance(v, str) else sum(len(repr(m)) // 4 for m in v)
    await memory.maintain(conversation_id, Complete(memory),
                          ContextRequest(conversation_id, 1000, count))

asyncio.run(main(*sys.argv[1:]))
"""


async def test_process_crash_inside_compaction_transaction(open_store, db: Path):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    messages = await fill(memory, conv.id, tokens(0, 40))
    await memory.aclose()

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _CRASH_SCRIPT, str(db), conv.id
    )
    assert await proc.wait() == 17

    reopened = open_store()
    view = await reopened.inspect(conv.id)
    assert view.summary == "" and view.long_term == [""]
    assert await active(reopened, conv.id) == messages
    await assert_archived(reopened, conv.id, messages)


async def test_repeated_consolidation(open_store, capsys):
    """Many compaction cycles: what the summary keeps versus what only the archive holds."""
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    completion = FakeCompletion(keep=6, render=tagged)
    anchor = Message(
        "anchor", Role.USER, (Text("tok9000 remember: deploy target is staging."),), time.time()
    )
    await memory.append(conv.id, [anchor])
    messages = [anchor]
    summary_tokens = []
    for cycle in range(50):
        messages += await fill(memory, conv.id, tokens(cycle * 20, 20))
        if cycle % 10 == 9:  # the user keeps bringing up one topic
            messages += await fill(memory, conv.id, ["tok9001"])
        await maintain(memory, conv.id, completion)
        summary_tokens.append(count_tokens((await memory.inspect(conv.id)).summary))

    view = await memory.inspect(conv.id)
    tail = await active(memory, conv.id)
    in_summary = set(TOKEN_RE.findall(view.summary))
    archive_only = [
        m for m in messages[: len(messages) - len(tail)]
        if not set(TOKEN_RE.findall(repr(m.blocks))) & in_summary
    ]
    assert len(completion.calls) == 50  # compactions only: no answer, so no saving
    assert max(summary_tokens) <= SUMMARY_TOKENS
    assert await memory.history(conv.id) == messages
    await assert_archived(memory, conv.id, archive_only)
    with capsys.disabled():
        print(
            f"\n[repeated consolidation] cycles=50 messages={len(messages)} tail={len(tail)}"
            f" summary_tokens={sorted(in_summary)}"
            f" archive_only={len(archive_only)} max_summary_tokens={max(summary_tokens)}"
        )


async def test_replayed_reasoning_counts_against_the_context_budget(open_store):
    """DeepSeek needs reasoning replayed, so the runtime's estimator must count it."""
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    thinking = Reasoning("x" * 4000, model="m")
    older = Message("a1", Role.ASSISTANT, (thinking, Text("ok")), 1.0)
    messages = [Message("u1", Role.USER, (Text("first"),), 0.0), older,
                Message("u2", Role.USER, (Text("second"),), 2.0)]
    await memory.append(conv.id, messages)
    assert estimate_tokens([older]) >= 1000

    roomy = await memory.assemble(ContextRequest(conv.id, 2000, estimate_tokens))
    assert thinking in next(m for m in roomy.messages if m.id == "a1").blocks

    tight = await memory.assemble(ContextRequest(conv.id, 500, estimate_tokens))
    assert [m.id for m in tight.messages] == ["u2"]
    assert estimate_tokens(tight.messages) <= 500


# ── Erasure ──────────────────────────────────────────────────────────────


def on_disk(db: Path) -> bytes:
    """Everything SQLite keeps for this store: the database file and its write-ahead log."""
    return b"".join(p.read_bytes() for p in (db, db.with_name(db.name + "-wal")) if p.exists())


async def test_secure_delete_is_on(open_store, db: Path):
    memory = open_store()
    await memory.create_conversation(title="test")
    assert await memory._run(lambda conn: conn.execute("PRAGMA secure_delete").fetchone()[0]) == 1


async def test_deleted_conversation_leaves_nothing_on_disk(open_store, db: Path):
    memory = open_store()
    kept = await memory.create_conversation(title="kept")
    doomed = await memory.create_conversation(title="quokkatitle")
    await fill(memory, kept.id, tokens(0, 3))
    secret = Message("s", Role.USER, (Text("remember: password is zanzibarquux"),), time.time())
    big = Message("b", Role.TOOL, (ToolResult("c", ToolStatus.OK, "wombatblob " * 2000),),
                  time.time())
    await memory.append(doomed.id, [secret, big])
    await fill(memory, doomed.id, tokens(100, 40))
    await memory.append(doomed.id, [answer()])
    await maintain(memory, doomed.id, FakeCompletion(render=lambda kept_, text: tagged(
        [*kept_, "summarysecretword"], text)))
    assert (await memory.notes())[0].text == "password is zanzibarquux"
    for word in (b"zanzibarquux", b"wombatblob", b"summarysecretword", b"quokkatitle"):
        assert word in on_disk(db)

    await memory.delete_conversation(doomed.id)
    for word in (b"wombatblob", b"summarysecretword", b"quokkatitle", doomed.id.encode()):
        assert word not in on_disk(db), word
    [profile] = await memory.notes()  # notes stay until deleted
    assert profile.text == "password is zanzibarquux"
    assert profile.source_conversation_id is None
    assert len(await memory.history(kept.id)) == 3
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError,
                                                    match="append-only"):
        conn.execute("DELETE FROM messages")  # the archive guard is back in force


async def test_a_deleted_note_leaves_nothing_on_disk(open_store, db: Path):
    memory = open_store()
    await memory.create_conversation(title="test")
    note = await memory.save_note("Locker", "The gym locker", "The code is marmalade.")
    await memory.save_note("Locker", "", "The code is pumpernickel.")
    with sqlite3.connect(db) as conn:  # the edit kept the earlier revision
        texts = [r[0] for r in conn.execute("SELECT text FROM note_revisions WHERE note_id = ?"
                                            " ORDER BY id", (int(note.id),))]
    assert texts == ["The code is marmalade.", "The code is pumpernickel."]
    await memory.delete_note(note.id)
    assert b"marmalade" not in on_disk(db) and b"pumpernickel" not in on_disk(db)


async def test_an_empty_seed_is_retried_rather_than_lost(open_store):
    """Continue from: when the seed summary comes back empty, a compaction must not
    write the first summary, or the source would never reach the conversation."""
    memory = open_store()
    source = await memory.create_conversation(title="source")
    await fill(memory, source.id, tokens(0, 30))
    seeded = await memory.create_conversation(title="seeded", seed_from=source.id)
    await fill(memory, seeded.id, tokens(500, 40))  # enough to compact on its own

    def empty_seed(kept: list[str], text: str) -> str:
        return "" if "tok0029" in text else tagged(kept, text)

    await maintain(memory, seeded.id, FakeCompletion(render=empty_seed))
    assert (await memory.inspect(seeded.id)).summary == ""
    # keep=200: the compaction that follows the seed keeps the source's tokens
    assert await maintain(memory, seeded.id, FakeCompletion(keep=200, render=tagged))
    assert "tok0001" in await addendum(memory, seeded.id)


async def test_only_the_newest_summary_is_kept(open_store, db: Path):
    """Old summaries are derived state that nothing reads; the messages stay."""
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    await fill(memory, conv.id, tokens(0, 40))
    assert await maintain(memory, conv.id, FakeCompletion(render=tagged))
    await fill(memory, conv.id, tokens(40, 40))
    assert await maintain(memory, conv.id, FakeCompletion(render=tagged))
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT conversation_id, through_seq FROM summaries").fetchall()
        assert len(rows) == 1 and rows[0][0] == conv.id
        assert rows[0][1] > 40  # the second summary's
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 80


async def test_long_tool_output_is_shortened_for_the_summary_not_the_archive(open_store):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    log = "BUILDSTART " + "x" * 50_000 + " BUILDEND"
    body = "PAGESTART " + "y" * 50_000
    exchange = [
        Message("call", Role.ASSISTANT, (ToolCall("c1", "write_file",
                                                  {"path": "a", "content": body}),), time.time()),
        Message("res", Role.TOOL, (ToolResult("c1", ToolStatus.OK, log),), time.time()),
    ]
    await fill(memory, conv.id, tokens(0, 2))
    await memory.append(conv.id, exchange)
    await fill(memory, conv.id, tokens(2, 40))
    completion = FakeCompletion(render=tagged)
    await maintain(memory, conv.id, completion)
    [summarized] = completion.calls
    sent = summarized["messages"][0].blocks[0].text
    assert "BUILDSTART" in sent and "BUILDEND" in sent and "PAGESTART" in sent
    assert "characters left out" in sent and len(sent) < 20_000
    stored = await memory.history(conv.id)
    assert ToolResult("c1", ToolStatus.OK, log) in [b for m in stored for b in m.blocks]


async def test_the_saving_pass_is_told_what_not_to_keep(open_store):
    """Notes hold what stays true and cannot be found again: not news of recent work, not
    what the files or the history already say, and a stale note is rewritten."""
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    await memory.append(conv.id, [text_message("remember: likes jazz"), answer()])
    completion = FakeCompletion(render=tagged)
    await maintain(memory, conv.id, completion)
    prompt = " ".join(completion.calls[0]["system"].split())
    for rule in ("news", "recent work", "files", "history", "no longer true"):
        assert rule in prompt, rule
