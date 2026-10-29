import asyncio
import re
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from kayden.api import ContextRequest, Message, Mode, Role, Text, ToolCall, ToolResult, ToolStatus
from kayden.memory_sqlite import SCHEMA_VERSION, CompactionConfig, SqliteMemory
from tests.conformance.memory_suite import (
    TOKEN_RE,
    FakeCompletion,
    MemoryConformance,
    active,
    addendum,
    assert_archived,
    count_tokens,
    text_message,
    tokens,
)

CONFIG = CompactionConfig(
    trigger_tokens=500, remove_fraction=0.5, summary_tokens=200, long_term_tokens=200
)
_FACT_RE = re.compile(r"remember: ([a-z ]+)")


def tagged(kept: list[str], text: str) -> str:
    facts = "".join(f"- {f}\n" for f in dict.fromkeys(_FACT_RE.findall(text)))
    return f"<summary>\nSummary: {' '.join(kept)}\n</summary>\n<long_term>\n{facts}</long_term>"


class TestSqliteMemory(MemoryConformance):
    @pytest.fixture
    def memory_factory(self, tmp_path: Path):
        return lambda: SqliteMemory(tmp_path / "memory.db", CONFIG)

    @pytest.fixture
    def completion(self) -> FakeCompletion:
        return FakeCompletion(render=tagged)


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


@pytest.mark.parametrize("fraction", [0.25, 0.5, 0.75])
async def test_remove_fraction_controls_how_much_is_compacted(open_store, fraction: float):
    config = CompactionConfig(trigger_tokens=500, remove_fraction=fraction, summary_tokens=200)
    memory = open_store(config)
    conv = await memory.create_conversation(Mode.SESSION)
    messages = await fill(memory, conv.id, tokens(0, 40))  # equal-sized messages
    await memory.maintain(conv.id, FakeCompletion(render=tagged))
    removed = len(messages) - len(await active(memory, conv.id))
    assert removed == round(len(messages) * fraction)


async def test_trigger_threshold_is_independent_of_fraction(open_store):
    memory = open_store(CompactionConfig(trigger_tokens=100_000, remove_fraction=0.5))
    conv = await memory.create_conversation(Mode.SESSION)
    messages = await fill(memory, conv.id, tokens(0, 40))
    completion = FakeCompletion(render=tagged)
    await memory.maintain(conv.id, completion)
    assert completion.calls == []
    assert await active(memory, conv.id) == messages


async def test_compaction_uses_light_model_and_summary_budget(open_store):
    memory = open_store()
    conv = await memory.create_conversation(Mode.SESSION)
    await fill(memory, conv.id, tokens(0, 40))
    completion = FakeCompletion(render=tagged)
    await memory.maintain(conv.id, completion)
    [call] = completion.calls
    assert call["light"] is True
    assert call["max_tokens"] >= CONFIG.summary_tokens
    assert "tok0000" in call["messages"][0].blocks[0].text
    assert "tok0039" not in call["messages"][0].blocks[0].text


async def test_summary_is_held_to_its_budget(open_store):
    memory = open_store()
    conv = await memory.create_conversation(Mode.SESSION)
    await fill(memory, conv.id, tokens(0, 40))
    await memory.maintain(conv.id, FakeCompletion(render=lambda k, t: "word " * 10_000))
    view = await memory.inspect(conv.id)
    assert 0 < len(view.summary) <= CONFIG.summary_tokens * 4
    assert view.long_term == []


async def test_long_term_entries_are_shared_within_a_mode(open_store):
    memory = open_store()
    first = await memory.create_conversation(Mode.SESSION)
    second = await memory.create_conversation(Mode.SESSION)
    continuous = await memory.create_conversation(Mode.CONTINUOUS)
    fact = Message("fact", Role.USER, (Text("remember: user prefers green tea."),), time.time())
    await memory.append(first.id, [fact])
    await fill(memory, first.id, tokens(0, 40))
    completion = FakeCompletion(render=tagged)
    await memory.maintain(first.id, completion)

    assert (await memory.inspect(first.id)).long_term == ["user prefers green tea"]
    assert "user prefers green tea" in await addendum(memory, second.id)
    assert "green tea" not in await addendum(memory, continuous.id)

    # Re-extracting a known fact does not duplicate it; entries survive a reopen.
    await memory.append(second.id, [fact])
    await fill(memory, second.id, tokens(100, 40))
    await memory.maintain(second.id, completion)
    await memory.aclose()
    reopened = open_store()
    assert (await reopened.inspect(second.id)).long_term == ["user prefers green tea"]


async def test_long_term_rendering_has_its_own_budget(open_store, db: Path):
    memory = open_store()
    conv = await memory.create_conversation(Mode.SESSION)
    await memory.append(conv.id, [text_message("tok0001")])
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO long_term (mode, text, source_conversation_id, created_at)"
            " VALUES ('session', ?, ?, 0)",
            [(f"fact number {i} " + "x" * 100, conv.id) for i in range(100)],
        )
    rendered = await addendum(memory, conv.id)
    assert "fact number 99" in rendered and "fact number 0 " not in rendered
    assert len(rendered) <= CONFIG.long_term_tokens * 4 + 100
    assert len((await memory.inspect(conv.id)).long_term) == 100


async def test_untagged_output_becomes_the_summary(open_store):
    memory = open_store()
    conv = await memory.create_conversation(Mode.SESSION)
    await fill(memory, conv.id, tokens(0, 40))
    await memory.maintain(conv.id, FakeCompletion(render=lambda k, t: "  plain summary  "))
    assert (await memory.inspect(conv.id)).summary == "plain summary"


async def test_compaction_never_strands_a_pending_tool_call(open_store):
    memory = open_store()
    conv = await memory.create_conversation(Mode.CONTINUOUS)
    before = await fill(memory, conv.id, tokens(0, 5))
    call = Message("call", Role.ASSISTANT, (ToolCall("c1", "slow_job", {}),), time.time())
    await memory.append(conv.id, [call])
    after = await fill(memory, conv.id, tokens(5, 40))
    await memory.maintain(conv.id, FakeCompletion(render=tagged))
    assert await active(memory, conv.id) == [call, *after]

    result = Message("res", Role.TOOL, (ToolResult("c1", ToolStatus.OK, "done"),), time.time())
    await memory.append(conv.id, [result])
    await memory.maintain(conv.id, FakeCompletion(render=tagged))
    tail = await active(memory, conv.id)
    assert (call in tail) == (result in tail)
    await assert_archived(memory, conv.id, before)


async def test_assemble_trims_whole_tool_exchanges(open_store):
    memory = open_store()
    conv = await memory.create_conversation(Mode.SESSION)
    call = Message("call", Role.ASSISTANT, (ToolCall("c1", "read", {}),), time.time())
    result = Message(
        "res", Role.TOOL, (ToolResult("c1", ToolStatus.OK, "x" * 400),), time.time()
    )
    final = Message("final", Role.ASSISTANT, (Text("done"),), time.time())
    await memory.append(conv.id, [call, result, final])
    budget = count_tokens([result, final])
    ctx = await memory.assemble(ContextRequest(conv.id, budget, count_tokens))
    assert list(ctx.messages) == [final]


async def test_append_rejects_duplicate_ids_atomically(open_store):
    memory = open_store()
    conv = await memory.create_conversation(Mode.SESSION)
    first = text_message("tok0001")
    await memory.append(conv.id, [first])
    with pytest.raises(sqlite3.IntegrityError):
        await memory.append(conv.id, [text_message("tok0002"), first])
    assert await memory.history(conv.id) == [first]


async def test_unknown_conversation_raises(open_store):
    memory = open_store()
    with pytest.raises(KeyError):
        await memory.append("missing", [text_message("tok0001")])
    with pytest.raises(KeyError):
        await memory.assemble(ContextRequest("missing", 100, count_tokens))


async def test_archive_is_append_only(open_store, db: Path):
    memory = open_store()
    conv = await memory.create_conversation(Mode.SESSION)
    await fill(memory, conv.id, tokens(0, 2))
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE messages SET blocks = '[]'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM messages")


async def test_unsupported_schema_version_is_refused(open_store, db: Path):
    memory = open_store()
    await memory.create_conversation(Mode.SESSION)
    await memory.aclose()
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="schema version 99"):
        await open_store().list_conversations(Mode.SESSION)


async def test_history_returns_the_full_transcript(open_store):
    memory = open_store()
    conv = await memory.create_conversation(Mode.SESSION)
    messages = await fill(memory, conv.id, tokens(0, 40))
    await memory.maintain(conv.id, FakeCompletion(render=tagged))
    assert len(await active(memory, conv.id)) < len(messages)
    assert await memory.history(conv.id) == messages


_CRASH_SCRIPT = """
import asyncio, os, sys
from kayden.memory_sqlite import CompactionConfig, SqliteMemory

async def main(path, conversation_id):
    memory = SqliteMemory(path, CompactionConfig(trigger_tokens=500, summary_tokens=200))

    def arm(conn):
        # Die as the compaction transaction starts to commit, after its writes ran.
        conn.set_trace_callback(lambda sql: sql == "COMMIT" and os._exit(17))

    async def complete(system, messages, *, max_tokens, light=False):
        await memory._run(arm)
        return "<summary>never committed</summary><long_term>\\n- lost fact\\n</long_term>"

    await memory.maintain(conversation_id, complete)

asyncio.run(main(*sys.argv[1:]))
"""


async def test_process_crash_inside_compaction_transaction(open_store, db: Path):
    memory = open_store()
    conv = await memory.create_conversation(Mode.CONTINUOUS)
    messages = await fill(memory, conv.id, tokens(0, 40))
    await memory.aclose()

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _CRASH_SCRIPT, str(db), conv.id
    )
    assert await proc.wait() == 17

    reopened = open_store()
    view = await reopened.inspect(conv.id)
    assert view.summary == "" and view.long_term == []
    assert await active(reopened, conv.id) == messages
    await assert_archived(reopened, conv.id, messages)


async def test_repeated_consolidation(open_store, capsys):
    """Many compaction cycles: what the summary keeps versus what only the archive holds."""
    memory = open_store()
    conv = await memory.create_conversation(Mode.CONTINUOUS)
    completion = FakeCompletion(keep=6, render=tagged)
    anchor = Message(
        "anchor", Role.USER, (Text("tok9000 remember: deploy target is staging."),), time.time()
    )
    await memory.append(conv.id, [anchor])
    messages = [anchor]
    summary_lengths = []
    for cycle in range(50):
        messages += await fill(memory, conv.id, tokens(cycle * 20, 20))
        if cycle % 10 == 9:  # the user keeps bringing up one topic
            messages += await fill(memory, conv.id, ["tok9001"])
        await memory.maintain(conv.id, completion)
        summary_lengths.append(len((await memory.inspect(conv.id)).summary))

    view = await memory.inspect(conv.id)
    tail = await active(memory, conv.id)
    in_summary = set(TOKEN_RE.findall(view.summary))
    archive_only = [
        m for m in messages[: len(messages) - len(tail)]
        if not set(TOKEN_RE.findall(repr(m.blocks))) & in_summary
    ]
    assert len(completion.calls) == 50
    assert max(summary_lengths) <= CONFIG.summary_tokens * 4
    assert view.long_term == ["deploy target is staging"]
    assert await memory.history(conv.id) == messages
    await assert_archived(memory, conv.id, archive_only)
    with capsys.disabled():
        print(
            f"\n[repeated consolidation] cycles=50 messages={len(messages)} tail={len(tail)}"
            f" summary_tokens={sorted(in_summary)} long_term={view.long_term}"
            f" archive_only={len(archive_only)} max_summary_chars={max(summary_lengths)}"
        )
