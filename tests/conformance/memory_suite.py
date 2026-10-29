"""Reusable conformance suite for the Memory contract.

Subclass `MemoryConformance` and override the `memory_factory` fixture with a callable
that opens the same persistent store on every call (calling it again = reopening the
store, as after a restart). maintain() is called with a window of `WINDOW` tokens; with
it the store must compact once the active context holds more than ~750 tokens
(`count_tokens`), and keep at least one recent message verbatim.

Override `completion` to make the fake summarizer's output match the implementation's
expected format. Whatever it renders, the summary must contain the tokens that
`FakeCompletion` keeps, so tests can follow what decays out of the summary.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from itertools import count
from typing import Any

import pytest

from kayden.api import (
    AssembledContext,
    BudgetExhausted,
    ContextRequest,
    Memory,
    Message,
    Mode,
    OpaqueBlock,
    OperationResult,
    Reasoning,
    Role,
    Text,
    ToolCall,
    ToolResult,
    ToolStatus,
)

type MemoryFactory = Callable[[], Memory]

UNLIMITED = 10**9
WINDOW = 1_000
TOKEN_RE = re.compile(r"\btok\d{4}\b")
_FILLER = (
    "The quick brown fox jumps over the lazy dog while the committee reviews budgets, "
    "timelines and the remaining open questions about the migration plan. "
)


def count_tokens(value: str | Sequence[Message]) -> int:
    """Deterministic stand-in for a provider tokenizer."""
    if isinstance(value, str):
        return len(value) // 4
    return sum(4 + sum(len(repr(b)) // 4 for b in m.blocks) for m in value)


def context(conversation_id: str, max_tokens: int = WINDOW) -> ContextRequest:
    return ContextRequest(conversation_id, max_tokens, count_tokens)


@dataclass
class FakeCompletion:
    """Summarizer that keeps only the `keep` most recently seen TOKEN_RE tokens.

    Repeated compaction therefore decays older tokens out of the summary.
    """

    keep: int = 3
    render: Callable[[list[str], str], str] = lambda kept, _text: "Summary: " + " ".join(kept)
    input_tokens: int = UNLIMITED  # max_input_tokens() of both models
    calls: list[dict] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # tokens kept by the latest call

    def max_input_tokens(self, *, light: bool = False) -> int:
        return self.input_tokens

    async def __call__(
        self, system: str, messages: Sequence[Message], *, max_tokens: int, light: bool = False
    ) -> str:
        text = system + "\n" + repr(list(messages))
        self.calls.append(
            {"system": system, "messages": messages, "light": light, "max_tokens": max_tokens}
        )
        seen = list(dict.fromkeys(reversed(TOKEN_RE.findall(text))))
        self.kept = list(reversed(seen[: self.keep]))
        return self.render(self.kept, text)


class Hanging:
    """Completion that blocks until released, to observe or abandon in-flight maintenance."""

    def __init__(self, inner: FakeCompletion) -> None:
        self.inner = inner
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def max_input_tokens(self, *, light: bool = False) -> int:
        return self.inner.max_input_tokens(light=light)

    async def __call__(
        self, system: str, messages: Sequence[Message], *, max_tokens: int, light: bool = False
    ) -> str:
        self.entered.set()
        await self.release.wait()
        return await self.inner(system, messages, max_tokens=max_tokens, light=light)

    async def wait_entered(self, task: asyncio.Task) -> None:
        """Fail instead of hanging if maintain() finishes without calling the completion."""
        entered = asyncio.ensure_future(self.entered.wait())
        await asyncio.wait({entered, task}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        if not entered.done():
            entered.cancel()
            if task.done():
                task.result()
            pytest.fail("maintain() never called the completion")


class Failing:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def max_input_tokens(self, *, light: bool = False) -> int:
        return UNLIMITED

    async def __call__(
        self, system: str, messages: Sequence[Message], *, max_tokens: int, light: bool = False
    ) -> str:
        raise self.error


_ids = count()


def text_message(token: str, role: Role = Role.USER) -> Message:
    return Message(f"m{next(_ids)}", role, (Text(f"{token} {_FILLER}"),), time.time())


def tool_exchange(token: str) -> list[Message]:
    call_id = f"call{next(_ids)}"
    return [
        Message(f"m{next(_ids)}", Role.ASSISTANT,
                (Reasoning("thinking", {"signature": "abc"}),
                 ToolCall(call_id, "lookup", {"query": token, "n": 3})), time.time()),
        Message(f"m{next(_ids)}", Role.TOOL,
                (ToolResult(call_id, ToolStatus.OK, f"result for {token} {_FILLER}"),),
                time.time()),
    ]


def tokens(start: int, n: int) -> list[str]:
    return [f"tok{i:04d}" for i in range(start, start + n)]


async def active(memory: Memory, conversation_id: str) -> list[Message]:
    ctx = await memory.assemble(context(conversation_id, UNLIMITED))
    return list(ctx.messages)


async def addendum(memory: Memory, conversation_id: str) -> str:
    ctx = await memory.assemble(context(conversation_id, UNLIMITED))
    return ctx.system_addendum


async def snapshot(memory: Memory, conversation_id: str) -> tuple:
    ctx = await memory.assemble(context(conversation_id, UNLIMITED))
    return ctx.system_addendum, list(ctx.messages), await memory.inspect(conversation_id)


def used_tokens(ctx: AssembledContext) -> int:
    return count_tokens(ctx.system_addendum) + count_tokens(ctx.messages)


def assert_well_formed(messages: Sequence[Message]) -> None:
    seen_calls = {b.id for m in messages for b in m.blocks if isinstance(b, ToolCall)}
    for m in messages:
        for b in m.blocks:
            if isinstance(b, ToolResult):
                assert b.call_id in seen_calls, f"orphaned tool result {b.call_id}"


async def assert_archived(memory: Memory, conversation_id: str, messages: Sequence[Message]):
    """Every message is retrievable from the archive with full fidelity."""
    for m in messages:
        token = TOKEN_RE.search(repr(m.blocks)).group()
        hits = await memory.search(token, conversation_id=conversation_id, limit=10)
        assert m in [h.message for h in hits], f"{m.id} ({token}) missing from archive"


class MemoryConformance:
    @pytest.fixture
    def memory_factory(self) -> MemoryFactory:
        raise NotImplementedError("override memory_factory")

    @pytest.fixture
    def completion(self) -> FakeCompletion:
        return FakeCompletion()

    @pytest.fixture
    async def open_memory(self, memory_factory: MemoryFactory) -> AsyncIterator[MemoryFactory]:
        opened: list[Memory] = []

        def open_() -> Memory:
            opened.append(memory_factory())
            return opened[-1]

        yield open_
        for memory in opened:
            await memory.aclose()

    @pytest.fixture
    def memory(self, open_memory: MemoryFactory) -> Memory:
        return open_memory()

    async def fill(self, memory: Memory, conversation_id: str, toks: Sequence[str]) -> list:
        messages = []
        for i, tok in enumerate(toks):
            batch = (
                tool_exchange(tok) if i % 5 == 4
                else [text_message(tok, Role.USER if i % 2 == 0 else Role.ASSISTANT)]
            )
            await memory.append(conversation_id, batch)
            messages.extend(batch)
        return messages

    async def maintain(self, memory: Memory, conversation_id: str, completion: Any) -> bool:
        return await memory.maintain(conversation_id, completion, context(conversation_id))

    # ── Conversations ─────────────────────────────────────────────────────

    async def test_conversations_are_listed_per_mode(self, memory: Memory):
        session = await memory.create_conversation(Mode.SESSION, "first")
        continuous = await memory.create_conversation(Mode.CONTINUOUS)
        assert session.mode is Mode.SESSION and session.title == "first"
        assert [c.id for c in await memory.list_conversations(Mode.SESSION)] == [session.id]
        assert [c.id for c in await memory.list_conversations(Mode.CONTINUOUS)] == [
            continuous.id
        ]

    async def test_unknown_conversation_raises_key_error(
        self, memory: Memory, completion: FakeCompletion
    ):
        calls = [
            memory.append("missing", [text_message("tok0001")]),
            memory.history("missing"),
            memory.assemble(context("missing")),
            memory.maintain("missing", completion, context("missing")),
            memory.search("tok0001", conversation_id="missing"),
            memory.inspect("missing"),
        ]
        for call in calls:
            with pytest.raises(KeyError):
                await call

    # ── Durability ────────────────────────────────────────────────────────

    async def test_append_is_durable_across_reopen(self, open_memory: MemoryFactory):
        memory = open_memory()
        conv = await memory.create_conversation(Mode.SESSION, "persisted")
        messages = await self.fill(memory, conv.id, tokens(0, 6))
        await memory.aclose()

        reopened = open_memory()
        assert await active(reopened, conv.id) == messages
        assert await reopened.history(conv.id) == messages
        assert [c.id for c in await reopened.list_conversations(Mode.SESSION)] == [conv.id]
        await assert_archived(reopened, conv.id, messages)

    async def test_append_is_durable_without_clean_shutdown(self, open_memory: MemoryFactory):
        memory = open_memory()
        conv = await memory.create_conversation(Mode.CONTINUOUS)
        messages = await self.fill(memory, conv.id, tokens(0, 4))
        # No aclose(): a second process-like instance must already see everything.
        assert await active(open_memory(), conv.id) == messages

    async def test_second_instance_reads_while_first_writes(self, open_memory: MemoryFactory):
        writer, reader = open_memory(), open_memory()
        conv = await writer.create_conversation(Mode.CONTINUOUS)
        await self.fill(writer, conv.id, tokens(0, 4))

        async def write() -> None:
            await self.fill(writer, conv.id, tokens(100, 20))

        async def read() -> None:
            for _ in range(20):
                await reader.search("tok0001", mode=Mode.CONTINUOUS)
                await reader.history(conv.id, limit=3)

        await asyncio.gather(write(), read())
        assert len(await reader.search("tok0118", mode=Mode.CONTINUOUS)) == 1

    async def test_all_block_types_round_trip(self, open_memory: MemoryFactory):
        memory = open_memory()
        conv = await memory.create_conversation(Mode.SESSION)
        messages = [
            Message("u", Role.USER, (Text("tok0001 go"),
                                     OperationResult("c0", ToolStatus.ABANDONED, "late")), 1.5),
            Message("a", Role.ASSISTANT, (
                Reasoning("hmm", {"signature": "s"}, "model-a"),
                ToolCall("c1", "lookup", {"q": ["x", 1]}),
                ToolCall("c2", "lookup", {}, raw_arguments="{not json"),
            ), 1_700_000_000.123456),
            Message("t", Role.TOOL, (ToolResult("c1", ToolStatus.OK, "found"),
                                     ToolResult("c2", ToolStatus.ERROR, "bad args")), 2.0),
        ]
        await memory.append(conv.id, messages)
        await memory.aclose()

        reopened = open_memory()
        assert await reopened.history(conv.id) == messages
        assert [h.message for h in await reopened.search("tok0001")] == [messages[0]]

    async def test_opaque_block_round_trips_unchanged(self, open_memory: MemoryFactory):
        memory = open_memory()
        conv = await memory.create_conversation(Mode.SESSION)
        message = Message(
            "future", Role.USER,
            (Text("tok0001 before"), OpaqueBlock("image", {"w": 3, "tags": ["a"], "r": 0.1}),
             Text("after")),
            1_700_000_000.123456,
        )
        await memory.append(conv.id, [message])
        await memory.aclose()

        reopened = open_memory()
        assert await reopened.history(conv.id) == [message]
        assert [h.message for h in await reopened.search("tok0001")] == [message]

    async def test_append_is_idempotent_by_message_id(self, memory: Memory):
        conv = await memory.create_conversation(Mode.SESSION)
        first, second = text_message("tok0001"), text_message("tok0002", Role.ASSISTANT)
        await memory.append(conv.id, [first])
        await memory.append(conv.id, [first, second])  # a cancelled append may have landed
        await memory.append(conv.id, [second])
        assert await memory.history(conv.id) == [first, second]

        changed = Message(first.id, Role.USER, (Text("tok0003 different"),), first.created_at)
        with pytest.raises(ValueError):
            await memory.append(conv.id, [changed])
        assert await memory.history(conv.id) == [first, second]

    # ── History ───────────────────────────────────────────────────────────

    async def test_history_pagination(self, memory: Memory, completion: FakeCompletion):
        conv = await memory.create_conversation(Mode.SESSION)
        messages = await self.fill(memory, conv.id, tokens(0, 30))
        assert await self.maintain(memory, conv.id, completion)
        assert await memory.history(conv.id) == messages  # regardless of compaction
        assert await memory.history(conv.id, limit=3) == messages[-3:]
        assert await memory.history(conv.id, before=messages[10].id) == messages[:10]
        assert await memory.history(conv.id, before=messages[10].id, limit=4) == messages[6:10]
        assert await memory.history(conv.id, before=messages[0].id) == []
        assert await memory.history(conv.id, limit=0) == []

    # ── Assemble ──────────────────────────────────────────────────────────

    @pytest.mark.parametrize("budget", [0, 40, 150, 600, 2_000, UNLIMITED])
    @pytest.mark.parametrize("maintained", [False, True])
    async def test_assemble_fits_budget(
        self, memory: Memory, completion: FakeCompletion, budget: int, maintained: bool
    ):
        conv = await memory.create_conversation(Mode.SESSION)
        await self.fill(memory, conv.id, tokens(0, 30))
        if maintained:
            assert await self.maintain(memory, conv.id, completion)
        # Deferred maintenance: far more history than compaction would normally allow.
        await self.fill(memory, conv.id, tokens(100, 30))
        full = await active(memory, conv.id)

        ctx = await memory.assemble(context(conv.id, budget))
        if budget >= 150:  # below that even the truncated newest exchange cannot fit
            assert used_tokens(ctx) <= budget
        assert ctx.messages and ctx.messages[0].role is Role.USER
        assert ctx.messages[-1].id == full[-1].id
        assert [m.id for m in ctx.messages] == [m.id for m in full[len(full) - len(ctx.messages):]]
        assert_well_formed(ctx.messages)
        if budget == UNLIMITED:
            assert list(ctx.messages) == full
        elif budget >= 2_000:
            assert len(ctx.messages) > 3, "a generous budget should keep more than the minimum"

    async def test_assemble_counts_the_addendum(self, memory: Memory, completion: FakeCompletion):
        conv = await memory.create_conversation(Mode.SESSION)
        await self.fill(memory, conv.id, tokens(0, 30))
        assert await self.maintain(memory, conv.id, completion)
        summary = await addendum(memory, conv.id)
        assert summary
        tail = await active(memory, conv.id)
        budget = count_tokens(summary) + count_tokens(tail)
        assert await memory.assemble(context(conv.id, budget)) == AssembledContext(summary, tail)
        ctx = await memory.assemble(context(conv.id, budget - 1))
        assert used_tokens(ctx) <= budget - 1

    async def test_assemble_starts_at_a_user_message(self, memory: Memory):
        conv = await memory.create_conversation(Mode.SESSION)
        call, result = tool_exchange("tok0001")
        u1, u2 = text_message("tok0000"), text_message("tok0002")
        final = text_message("tok0003", Role.ASSISTANT)
        messages = [u1, call, result, u2, final]
        await memory.append(conv.id, messages)
        for budget in range(count_tokens([u2, final]), count_tokens(messages) + 1):
            ctx = await memory.assemble(context(conv.id, budget))
            assert list(ctx.messages) in ([u2, final], messages)
            assert used_tokens(ctx) <= budget

    async def test_oversized_newest_message_is_truncated_in_view_only(self, memory: Memory):
        conv = await memory.create_conversation(Mode.SESSION)
        earlier = await self.fill(memory, conv.id, tokens(0, 4))
        huge = Message("huge", Role.USER, (Text("tok0500 " + "lorem ipsum " * 2_000),), 1.0)
        await memory.append(conv.id, [huge])

        ctx = await memory.assemble(context(conv.id, 500))
        assert used_tokens(ctx) <= 500
        assert [m.id for m in ctx.messages] == ["huge"]
        shown = ctx.messages[0].blocks[0].text
        assert shown.startswith("tok0500") and "huge" in shown and len(shown) < 2_000
        assert await memory.history(conv.id) == [*earlier, huge]

    # ── Maintenance ───────────────────────────────────────────────────────

    async def test_maintain_skips_small_context(self, memory: Memory, completion: FakeCompletion):
        conv = await memory.create_conversation(Mode.SESSION)
        messages = await self.fill(memory, conv.id, tokens(0, 2))
        assert await self.maintain(memory, conv.id, completion) is False
        assert completion.calls == []
        assert await active(memory, conv.id) == messages

    async def test_maintain_threshold_follows_the_window(
        self, memory: Memory, completion: FakeCompletion
    ):
        conv = await memory.create_conversation(Mode.SESSION)
        await self.fill(memory, conv.id, tokens(0, 30))
        assert not await memory.maintain(conv.id, completion, context(conv.id, 100 * WINDOW))
        assert completion.calls == []
        assert await memory.maintain(conv.id, completion, context(conv.id, WINDOW))

    async def test_maintain_compacts_and_archives_first(
        self, memory: Memory, completion: FakeCompletion
    ):
        conv = await memory.create_conversation(Mode.SESSION)
        messages = await self.fill(memory, conv.id, tokens(0, 30))
        assert await self.maintain(memory, conv.id, completion) is True

        tail = await active(memory, conv.id)
        assert completion.calls, "maintain should have compacted"
        assert 0 < len(tail) < len(messages)
        assert tail == messages[len(messages) - len(tail):]
        assert tail[0].role is Role.USER
        assert_well_formed(tail)
        summary = await addendum(memory, conv.id)
        assert completion.kept and all(t in summary for t in completion.kept)
        shown = TOKEN_RE.findall(repr(completion.calls[-1]))
        removed = messages[: len(messages) - len(tail)]
        assert all(TOKEN_RE.search(repr(m.blocks)).group() in shown for m in removed), (
            "a message left active context without being shown to the summarizer"
        )
        await assert_archived(memory, conv.id, messages[: len(messages) - len(tail)])

    async def test_maintain_sizes_its_request_to_the_light_model(self, memory: Memory):
        conv = await memory.create_conversation(Mode.SESSION)
        await self.fill(memory, conv.id, tokens(0, 60))
        completion = FakeCompletion(input_tokens=1_200)
        assert await self.maintain(memory, conv.id, completion)
        [call] = completion.calls
        assert call["light"] is True
        sent = count_tokens(call["system"]) + count_tokens(call["messages"])
        assert sent + call["max_tokens"] <= 1_200

    async def test_failed_completion_changes_nothing(
        self, memory: Memory, completion: FakeCompletion
    ):
        conv = await memory.create_conversation(Mode.SESSION)
        await self.fill(memory, conv.id, tokens(0, 30))
        before = await snapshot(memory, conv.id)
        with pytest.raises(RuntimeError):
            await self.maintain(memory, conv.id, Failing(RuntimeError("provider down")))
        with pytest.raises(BudgetExhausted):
            await self.maintain(memory, conv.id, Failing(BudgetExhausted("spent")))
        assert await snapshot(memory, conv.id) == before

    async def test_crash_during_compaction_loses_nothing(
        self, open_memory: MemoryFactory, completion: FakeCompletion
    ):
        memory = open_memory()
        conv = await memory.create_conversation(Mode.CONTINUOUS)
        messages = await self.fill(memory, conv.id, tokens(0, 30))
        await self.maintain(memory, conv.id, completion)
        messages += await self.fill(memory, conv.id, tokens(100, 30))
        before = await snapshot(memory, conv.id)

        hanging = Hanging(completion)
        task = asyncio.create_task(self.maintain(memory, conv.id, hanging))
        await hanging.wait_entered(task)
        task.cancel()  # the process dies mid-compaction; nothing is shut down cleanly
        with pytest.raises(asyncio.CancelledError):
            await task

        survivor = open_memory()
        assert await snapshot(survivor, conv.id) == before
        await assert_archived(survivor, conv.id, messages)
        await self.maintain(survivor, conv.id, completion)
        await assert_archived(survivor, conv.id, messages)

    async def test_archive_finds_messages_decayed_out_of_summary(
        self, memory: Memory, completion: FakeCompletion
    ):
        conv = await memory.create_conversation(Mode.CONTINUOUS)
        messages = []
        for cycle in range(8):
            messages += await self.fill(memory, conv.id, tokens(cycle * 20, 20))
            await self.maintain(memory, conv.id, completion)
        assert len(completion.calls) >= 4

        tail = await active(memory, conv.id)
        summary = await addendum(memory, conv.id)
        decayed = [
            m for m in messages[: len(messages) - len(tail)]
            if TOKEN_RE.search(repr(m.blocks)).group() not in summary
        ]
        assert len(decayed) > len(messages) // 2
        await assert_archived(memory, conv.id, decayed)

    # ── Mode isolation ────────────────────────────────────────────────────

    async def test_modes_are_isolated(self, memory: Memory, completion: FakeCompletion):
        session = await memory.create_conversation(Mode.SESSION)
        continuous = await memory.create_conversation(Mode.CONTINUOUS)
        await self.fill(memory, session.id, tokens(0, 30))
        await self.fill(memory, continuous.id, tokens(500, 30))
        before = await snapshot(memory, continuous.id)

        await self.maintain(memory, session.id, completion)
        assert await snapshot(memory, continuous.id) == before
        assert not set(TOKEN_RE.findall(before[0])) & set(tokens(0, 30))

        session_hits = await memory.search("tok0003", mode=Mode.SESSION)
        assert {h.conversation_id for h in session_hits} == {session.id}
        assert await memory.search("tok0003", mode=Mode.CONTINUOUS) == []
        assert await memory.search("tok0503", mode=Mode.SESSION) == []
        assert await memory.search("tok0503", conversation_id=session.id) == []
        both = await memory.search("tok0003 tok0503", limit=10)
        assert {h.conversation_id for h in both} == {session.id, continuous.id}

    async def test_cross_mode_search_is_read_only(
        self, memory: Memory, completion: FakeCompletion
    ):
        session = await memory.create_conversation(Mode.SESSION)
        continuous = await memory.create_conversation(Mode.CONTINUOUS)
        await self.fill(memory, continuous.id, tokens(0, 30))
        await self.maintain(memory, continuous.id, completion)
        before = await snapshot(memory, continuous.id)
        listed = await memory.list_conversations(Mode.CONTINUOUS)

        for query in ("tok0001", "fox committee", "", "\"unbalanced ( * OR", "a:b NEAR(c)"):
            await memory.search(query, mode=Mode.CONTINUOUS)
        assert await snapshot(memory, continuous.id) == before
        assert await memory.list_conversations(Mode.CONTINUOUS) == listed
        assert await active(memory, session.id) == []

    async def test_search_respects_limit(self, memory: Memory):
        conv = await memory.create_conversation(Mode.SESSION)
        await self.fill(memory, conv.id, tokens(0, 10))
        assert len(await memory.search("fox", limit=3)) == 3
        assert await memory.search("nonexistentword") == []

    async def test_search_higher_score_is_better(self, memory: Memory):
        conv = await memory.create_conversation(Mode.SESSION)
        await self.fill(memory, conv.id, tokens(0, 6))
        both = Message("both", Role.USER, (Text("zebra giraffe"),), time.time())
        one = Message("one", Role.USER, (Text("zebra and other words entirely"),), time.time())
        await memory.append(conv.id, [one, both])
        hits = await memory.search("zebra giraffe")
        assert [h.message.id for h in hits] == ["both", "one"]
        if hits[0].score is not None:
            assert hits[0].score > hits[1].score

    # ── Inspect ───────────────────────────────────────────────────────────

    async def test_inspect(self, memory: Memory, completion: FakeCompletion):
        conv = await memory.create_conversation(Mode.SESSION)
        await self.fill(memory, conv.id, tokens(0, 30))
        view = await memory.inspect(conv.id)
        if view is None:
            pytest.skip("inspect is optional and not implemented")
        assert view.summary == ""

        await self.maintain(memory, conv.id, completion)
        view = await memory.inspect(conv.id)
        assert view.summary and view.summary in await addendum(memory, conv.id)
        assert await memory.inspect(conv.id) == view

    # ── Serialization ─────────────────────────────────────────────────────

    async def test_concurrent_appends_keep_order(self, memory: Memory):
        a = await memory.create_conversation(Mode.SESSION)
        b = await memory.create_conversation(Mode.SESSION)
        batches = [[text_message(t)] for t in tokens(0, 20)]
        await asyncio.gather(
            *(memory.append(conv.id, batch) for batch in batches for conv in (a, b))
        )
        expected = [m for batch in batches for m in batch]
        assert await active(memory, a.id) == expected
        assert await active(memory, b.id) == expected

    async def test_reads_during_maintenance_see_a_consistent_state(
        self, memory: Memory, completion: FakeCompletion
    ):
        conv = await memory.create_conversation(Mode.SESSION)
        messages = await self.fill(memory, conv.id, tokens(0, 30))
        before = await snapshot(memory, conv.id)

        hanging = Hanging(completion)
        task = asyncio.create_task(self.maintain(memory, conv.id, hanging))
        await hanging.wait_entered(task)
        assert await snapshot(memory, conv.id) == before
        late = [text_message("tok0999")]
        append = asyncio.create_task(memory.append(conv.id, late))
        await asyncio.sleep(0.01)
        hanging.release.set()
        await asyncio.gather(task, append)

        tail = await active(memory, conv.id)
        assert tail[-1] == late[0]
        assert len(tail) < len(messages)
        assert tail[:-1] == messages[len(messages) - len(tail) + 1:]
        await assert_archived(memory, conv.id, messages + late)
