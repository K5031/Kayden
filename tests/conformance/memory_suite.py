"""Reusable conformance suite for the Memory contract.

Subclass `MemoryConformance` and override the `memory_factory` fixture with a callable
that opens the same persistent store on every call (calling it again = reopening the
store, as after a restart). The store must be configured so that maintain() compacts
whenever the active verbatim context holds more than ~2,000 characters of text, and
keeps at least one recent message verbatim.

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

import pytest

from kayden.api import (
    JSON,
    BudgetExhausted,
    ContextRequest,
    Memory,
    Message,
    Mode,
    Reasoning,
    Role,
    Text,
    ToolCall,
    ToolResult,
    ToolStatus,
)

type MemoryFactory = Callable[[], Memory]

UNLIMITED = 10**9
TOKEN_RE = re.compile(r"\btok\d{4}\b")
_FILLER = (
    "The quick brown fox jumps over the lazy dog while the committee reviews budgets, "
    "timelines and the remaining open questions about the migration plan. "
)


def count_tokens(messages: Sequence[Message]) -> int:
    """Deterministic stand-in for a provider tokenizer."""
    return sum(4 + sum(len(repr(b)) // 4 for b in m.blocks) for m in messages)


@dataclass(frozen=True, slots=True)
class FutureBlock:
    """A block type the Memory implementation has never seen."""

    kind: str
    payload: JSON
    ratio: float


@dataclass
class FakeCompletion:
    """Summarizer that keeps only the `keep` most recently seen TOKEN_RE tokens.

    Repeated compaction therefore decays older tokens out of the summary.
    """

    keep: int = 3
    render: Callable[[list[str], str], str] = lambda kept, _text: "Summary: " + " ".join(kept)
    calls: list[dict] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # tokens kept by the latest call

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
    ctx = await memory.assemble(ContextRequest(conversation_id, UNLIMITED, count_tokens))
    return list(ctx.messages)


async def addendum(memory: Memory, conversation_id: str) -> str:
    ctx = await memory.assemble(ContextRequest(conversation_id, UNLIMITED, count_tokens))
    return ctx.system_addendum


async def snapshot(memory: Memory, conversation_id: str) -> tuple:
    ctx = await memory.assemble(ContextRequest(conversation_id, UNLIMITED, count_tokens))
    return ctx.system_addendum, list(ctx.messages), await memory.inspect(conversation_id)


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

    # ── Conversations ─────────────────────────────────────────────────────

    async def test_conversations_are_listed_per_mode(self, memory: Memory):
        session = await memory.create_conversation(Mode.SESSION, "first")
        continuous = await memory.create_conversation(Mode.CONTINUOUS)
        assert session.mode is Mode.SESSION and session.title == "first"
        assert [c.id for c in await memory.list_conversations(Mode.SESSION)] == [session.id]
        assert [c.id for c in await memory.list_conversations(Mode.CONTINUOUS)] == [
            continuous.id
        ]

    # ── Durability ────────────────────────────────────────────────────────

    async def test_append_is_durable_across_reopen(self, open_memory: MemoryFactory):
        memory = open_memory()
        conv = await memory.create_conversation(Mode.SESSION, "persisted")
        messages = await self.fill(memory, conv.id, tokens(0, 6))
        await memory.aclose()

        reopened = open_memory()
        assert await active(reopened, conv.id) == messages
        assert [c.id for c in await reopened.list_conversations(Mode.SESSION)] == [conv.id]
        await assert_archived(reopened, conv.id, messages)

    async def test_append_is_durable_without_clean_shutdown(self, open_memory: MemoryFactory):
        memory = open_memory()
        conv = await memory.create_conversation(Mode.CONTINUOUS)
        messages = await self.fill(memory, conv.id, tokens(0, 4))
        # No aclose(): a second process-like instance must already see everything.
        assert await active(open_memory(), conv.id) == messages

    async def test_unknown_block_type_round_trips(self, open_memory: MemoryFactory):
        memory = open_memory()
        conv = await memory.create_conversation(Mode.SESSION)
        message = Message(
            "future", Role.ASSISTANT,
            (Text("tok0001 before"), FutureBlock("image", {"w": 3, "tags": ["a"]}, 0.1),
             Text("after")),
            1_700_000_000.123456,
        )
        await memory.append(conv.id, [message])
        await memory.aclose()

        reopened = open_memory()
        assert await active(reopened, conv.id) == [message]
        hits = await reopened.search("tok0001")
        assert [h.message for h in hits] == [message]

    # ── Assemble ──────────────────────────────────────────────────────────

    @pytest.mark.parametrize("budget", [0, 40, 150, 600, 2_000, UNLIMITED])
    @pytest.mark.parametrize("maintained", [False, True])
    async def test_assemble_fits_budget(
        self, memory: Memory, completion: FakeCompletion, budget: int, maintained: bool
    ):
        conv = await memory.create_conversation(Mode.SESSION)
        await self.fill(memory, conv.id, tokens(0, 30))
        if maintained:
            await memory.maintain(conv.id, completion)
        # Deferred maintenance: far more history than compaction would normally allow.
        await self.fill(memory, conv.id, tokens(100, 30))
        full = await active(memory, conv.id)

        ctx = await memory.assemble(ContextRequest(conv.id, budget, count_tokens))
        rendered = [Message("a", Role.USER, (Text(ctx.system_addendum),), 0.0)]
        used = count_tokens([*(rendered if ctx.system_addendum else []), *ctx.messages])
        assert used <= budget
        assert list(ctx.messages) == full[len(full) - len(ctx.messages):]
        assert_well_formed(ctx.messages)
        if budget == UNLIMITED:
            assert len(ctx.messages) == len(full)
        elif budget >= 2_000:
            assert ctx.messages, "a generous budget should keep some verbatim tail"

    async def test_assemble_never_orphans_a_tool_result(self, memory: Memory):
        conv = await memory.create_conversation(Mode.SESSION)
        call, result = tool_exchange("tok0001")
        final = text_message("tok0002", Role.ASSISTANT)
        await memory.append(conv.id, [call, result, final])
        for budget in range(count_tokens([final]), count_tokens([call, result, final]) + 1):
            ctx = await memory.assemble(ContextRequest(conv.id, budget, count_tokens))
            assert_well_formed(ctx.messages)
            assert count_tokens(ctx.messages) <= budget

    async def test_assemble_is_read_only(self, memory: Memory, completion: FakeCompletion):
        conv = await memory.create_conversation(Mode.SESSION)
        await self.fill(memory, conv.id, tokens(0, 30))
        await memory.maintain(conv.id, completion)
        before = await snapshot(memory, conv.id)
        listed = await memory.list_conversations(Mode.SESSION)
        for budget in (10, 300, UNLIMITED):
            await memory.assemble(ContextRequest(conv.id, budget, count_tokens))
        assert await snapshot(memory, conv.id) == before
        assert await memory.list_conversations(Mode.SESSION) == listed

    # ── Maintenance ───────────────────────────────────────────────────────

    async def test_maintain_skips_small_context(self, memory: Memory, completion: FakeCompletion):
        conv = await memory.create_conversation(Mode.SESSION)
        messages = await self.fill(memory, conv.id, tokens(0, 2))
        await memory.maintain(conv.id, completion)
        assert completion.calls == []
        assert await active(memory, conv.id) == messages

    async def test_maintain_compacts_and_archives_first(
        self, memory: Memory, completion: FakeCompletion
    ):
        conv = await memory.create_conversation(Mode.SESSION)
        messages = await self.fill(memory, conv.id, tokens(0, 30))
        await memory.maintain(conv.id, completion)

        tail = await active(memory, conv.id)
        assert completion.calls, "maintain should have compacted"
        assert 0 < len(tail) < len(messages)
        assert tail == messages[len(messages) - len(tail):]
        assert_well_formed(tail)
        summary = await addendum(memory, conv.id)
        assert completion.kept and all(t in summary for t in completion.kept)
        shown = TOKEN_RE.findall(repr(completion.calls[-1]))
        removed = messages[: len(messages) - len(tail)]
        assert all(TOKEN_RE.search(repr(m.blocks)).group() in shown for m in removed), (
            "a message left active context without being shown to the summarizer"
        )
        await assert_archived(memory, conv.id, messages[: len(messages) - len(tail)])

    async def test_failed_completion_changes_nothing(
        self, memory: Memory, completion: FakeCompletion
    ):
        conv = await memory.create_conversation(Mode.SESSION)
        await self.fill(memory, conv.id, tokens(0, 30))
        before = await snapshot(memory, conv.id)
        with pytest.raises(RuntimeError):
            await memory.maintain(conv.id, Failing(RuntimeError("provider down")))
        try:
            await memory.maintain(conv.id, Failing(BudgetExhausted("spent")))
        except BudgetExhausted:
            pass
        assert await snapshot(memory, conv.id) == before

    async def test_crash_during_compaction_loses_nothing(
        self, open_memory: MemoryFactory, completion: FakeCompletion
    ):
        memory = open_memory()
        conv = await memory.create_conversation(Mode.CONTINUOUS)
        messages = await self.fill(memory, conv.id, tokens(0, 30))
        await memory.maintain(conv.id, completion)
        messages += await self.fill(memory, conv.id, tokens(100, 30))
        before = await snapshot(memory, conv.id)

        hanging = Hanging(completion)
        task = asyncio.create_task(memory.maintain(conv.id, hanging))
        await hanging.wait_entered(task)
        task.cancel()  # the process dies mid-compaction; nothing is shut down cleanly
        with pytest.raises(asyncio.CancelledError):
            await task

        survivor = open_memory()
        assert await snapshot(survivor, conv.id) == before
        await assert_archived(survivor, conv.id, messages)
        await survivor.maintain(conv.id, completion)
        await assert_archived(survivor, conv.id, messages)

    async def test_archive_finds_messages_decayed_out_of_summary(
        self, memory: Memory, completion: FakeCompletion
    ):
        conv = await memory.create_conversation(Mode.CONTINUOUS)
        messages = []
        for cycle in range(8):
            messages += await self.fill(memory, conv.id, tokens(cycle * 20, 20))
            await memory.maintain(conv.id, completion)
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

        await memory.maintain(session.id, completion)
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
        await memory.maintain(continuous.id, completion)
        before = await snapshot(memory, continuous.id)
        listed = await memory.list_conversations(Mode.CONTINUOUS)

        for query in ("tok0001", "fox committee", "", "\"unbalanced ( * OR"):
            await memory.search(query, mode=Mode.CONTINUOUS)
        assert await snapshot(memory, continuous.id) == before
        assert await memory.list_conversations(Mode.CONTINUOUS) == listed
        assert await active(memory, session.id) == []

    async def test_search_respects_limit(self, memory: Memory):
        conv = await memory.create_conversation(Mode.SESSION)
        await self.fill(memory, conv.id, tokens(0, 10))
        assert len(await memory.search("fox", limit=3)) == 3
        assert await memory.search("nonexistentword") == []

    # ── Inspect ───────────────────────────────────────────────────────────

    async def test_inspect(self, memory: Memory, completion: FakeCompletion):
        conv = await memory.create_conversation(Mode.SESSION)
        await self.fill(memory, conv.id, tokens(0, 30))
        view = await memory.inspect(conv.id)
        if view is None:
            pytest.skip("inspect is optional and not implemented")
        assert view.summary == ""

        await memory.maintain(conv.id, completion)
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
        task = asyncio.create_task(memory.maintain(conv.id, hanging))
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
