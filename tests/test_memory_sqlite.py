import asyncio
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from kayden.api import (
    ContextRequest,
    InstancePaths,
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
    active,
    addendum,
    assert_archived,
    context,
    count_tokens,
    text_message,
    tokens,
)

CONFIG = CompactionConfig(trigger_fraction=0.75, remove_fraction=0.5, summary_fraction=0.05,
                          long_term_fraction=0.05)
SUMMARY_TOKENS = CONFIG.summary_fraction * WINDOW
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
    assert view.long_term == []


async def test_long_term_entries_are_shared_by_conversations_that_remember(open_store):
    memory = open_store()
    first = await memory.create_conversation(title="test")
    second = await memory.create_conversation(title="test")
    private = await memory.create_conversation(title="test", remember=False)
    fact = Message("fact", Role.USER, (Text("remember: user prefers green tea."),), time.time())
    await memory.append(first.id, [fact])
    await fill(memory, first.id, tokens(0, 40))
    completion = FakeCompletion(render=tagged)
    await maintain(memory, first.id, completion)

    assert (await memory.inspect(first.id)).long_term == ["user prefers green tea"]
    assert "user prefers green tea" in await addendum(memory, second.id)
    assert "green tea" not in await addendum(memory, private.id)

    # Re-extracting a known fact does not duplicate it; entries survive a reopen.
    fact2 = Message("fact2", Role.USER, fact.blocks, time.time())
    await memory.append(second.id, [fact2])
    await fill(memory, second.id, tokens(100, 40))
    await maintain(memory, second.id, completion)
    await memory.aclose()
    reopened = open_store()
    assert (await reopened.inspect(second.id)).long_term == ["user prefers green tea"]


async def test_long_term_rendering_is_a_fraction_of_the_budget(open_store, db: Path):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    await memory.append(conv.id, [text_message("tok0001")])
    for i in range(100):
        await memory.remember(f"fact number {i} " + "x" * 100)
    budget = 4_000
    rendered = (await memory.assemble(context(conv.id, budget))).system_addendum
    assert "fact number 99" in rendered and "fact number 0 " not in rendered
    assert count_tokens(rendered) <= CONFIG.long_term_fraction * budget + 10
    assert len((await memory.inspect(conv.id)).long_term) == 100


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

    async def __call__(self, system, messages, *, max_tokens, light=False):
        def arm(conn):
            # Die as the compaction transaction starts to commit, after its writes ran.
            conn.set_trace_callback(lambda sql: sql == "COMMIT" and os._exit(17))
        await self.memory._run(arm)
        return "<summary>never committed</summary><long_term>\\n- lost fact\\n</long_term>"

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
    assert view.summary == "" and view.long_term == []
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
    assert len(completion.calls) == 50
    assert max(summary_tokens) <= SUMMARY_TOKENS
    assert view.long_term == ["deploy target is staging"]
    assert await memory.history(conv.id) == messages
    await assert_archived(memory, conv.id, archive_only)
    with capsys.disabled():
        print(
            f"\n[repeated consolidation] cycles=50 messages={len(messages)} tail={len(tail)}"
            f" summary_tokens={sorted(in_summary)} long_term={view.long_term}"
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
    await maintain(memory, doomed.id, FakeCompletion(render=lambda kept_, text: tagged(
        [*kept_, "summarysecretword"], text)))
    assert (await memory.memories())[0].text == "password is zanzibarquux"
    for word in (b"zanzibarquux", b"wombatblob", b"summarysecretword", b"quokkatitle"):
        assert word in on_disk(db)

    await memory.delete_conversation(doomed.id)
    for word in (b"zanzibarquux", b"wombatblob", b"summarysecretword", b"quokkatitle",
                 doomed.id.encode()):
        assert word not in on_disk(db), word
    assert await memory.memories() == []
    assert len(await memory.history(kept.id)) == 3
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError,
                                                    match="append-only"):
        conn.execute("DELETE FROM messages")  # the archive guard is back in force


async def test_forgotten_memory_leaves_nothing_on_disk(open_store, db: Path):
    memory = open_store()
    await memory.create_conversation(title="test")
    entry = await memory.remember("The user's locker code is marmalade.")
    await memory.revise(entry.id, "The user's locker code is pumpernickel.")
    with sqlite3.connect(db) as conn:  # the edit kept the earlier revision
        texts = [r[0] for r in conn.execute("SELECT text FROM memory_revisions ORDER BY id")]
    assert texts == ["The user's locker code is marmalade.",
                     "The user's locker code is pumpernickel."]
    await memory.forget(entry.id)
    assert b"marmalade" not in on_disk(db) and b"pumpernickel" not in on_disk(db)


async def test_forgotten_fact_is_not_re_extracted(open_store):
    memory = open_store()
    conv = await memory.create_conversation(title="test")
    fact = Text("remember: user prefers green tea.")
    completion = FakeCompletion(render=tagged)
    await memory.append(conv.id, [Message("f1", Role.USER, (fact,), time.time())])
    await fill(memory, conv.id, tokens(0, 40))
    await maintain(memory, conv.id, completion)
    [entry] = await memory.memories()
    await memory.forget(entry.id)

    await memory.append(conv.id, [Message("f2", Role.USER, (fact,), time.time())])
    await fill(memory, conv.id, tokens(100, 40))
    await maintain(memory, conv.id, completion)
    assert "green tea" in completion.calls[-1]["messages"][0].blocks[0].text
    assert await memory.memories() == []
    new = Message("f3", Role.USER, (Text("remember: user prefers black coffee."),), time.time())
    await memory.append(conv.id, [new])
    await fill(memory, conv.id, tokens(200, 40))
    await maintain(memory, conv.id, completion)
    assert [e.text for e in await memory.memories()] == ["user prefers black coffee"]


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
