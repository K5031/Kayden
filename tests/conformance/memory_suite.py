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
from pathlib import Path
from typing import Any

import pytest

from kadence.api import (
    AssembledContext,
    BudgetExhausted,
    ContextRequest,
    CoreError,
    Duty,
    DutyKind,
    Memory,
    Message,
    OpaqueBlock,
    OperationResult,
    PlanItem,
    PlanStatus,
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
        self, system: str, messages: Sequence[Message], *, max_tokens: int, light: bool = False,
        compacting: bool = False,
    ) -> str:
        text = system + "\n" + repr(list(messages))
        self.calls.append(
            {"system": system, "messages": messages, "light": light, "max_tokens": max_tokens,
             "compacting": compacting}
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
        self, system: str, messages: Sequence[Message], *, max_tokens: int, light: bool = False,
        compacting: bool = False,
    ) -> str:
        self.entered.set()
        await self.release.wait()
        return await self.inner(system, messages, max_tokens=max_tokens, light=light,
                                compacting=compacting)

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
        self, system: str, messages: Sequence[Message], *, max_tokens: int, light: bool = False,
        compacting: bool = False,
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
        hits = await memory.search(token, conversation_ids=[conversation_id], limit=10)
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

    async def test_conversations_are_listed_newest_first(self, memory: Memory):
        first = await memory.create_conversation(title="first")
        second = await memory.create_conversation()
        assert first.title == "first" and second.title is None
        assert not first.archived
        assert first.seed_from is None
        await memory.append(first.id, [text_message("tok0001")])
        listed = await memory.list_conversations()
        assert [c.id for c in listed] == [first.id, second.id]
        assert listed[0].preview and listed[0].preview.startswith("tok0001")
        assert listed[1].preview is None

    async def test_conversation_options_are_stored(self, open_memory: MemoryFactory):
        memory = open_memory()
        source = await memory.create_conversation(title="source")
        conv = await memory.create_conversation(seed_from=source.id, folder="/work/trip")
        assert conv.seed_from == source.id and conv.folder == "/work/trip"
        assert source.folder is None
        with pytest.raises(KeyError):
            await memory.create_conversation(seed_from="missing")
        await memory.aclose()
        [reopened] = [c for c in await open_memory().list_conversations() if c.id == conv.id]
        assert reopened.seed_from == source.id and reopened.folder == "/work/trip"

    async def test_a_conversation_can_be_created_with_a_given_id(self, memory: Memory):
        conv = await memory.create_conversation(conversation_id="chosen1", folder="/work")
        assert (conv.id, conv.folder) == ("chosen1", "/work")
        assert [c.id for c in await memory.list_conversations()] == ["chosen1"]
        with pytest.raises(ValueError):
            await memory.create_conversation(conversation_id="chosen1")

    async def test_update_conversation(self, memory: Memory):
        conv = await memory.create_conversation()
        renamed = await memory.update_conversation(conv.id, title="Trip")
        assert renamed.title == "Trip" and renamed.id == conv.id
        assert (await memory.update_conversation(conv.id, title="")).title is None
        await memory.update_conversation(conv.id, title="Trip")
        assert (await memory.update_conversation(conv.id, title="  \n")).title is None
        [listed] = await memory.list_conversations()
        assert listed.title is None
        with pytest.raises(KeyError):
            await memory.update_conversation("missing", title="x")

    async def test_a_duty_and_plan_are_stored_and_not_copied(self, open_memory: MemoryFactory):
        memory = open_memory()
        conv = await memory.create_conversation()
        assert conv.duty is None and conv.plan == ()
        duty = Duty("Answer the inbox", DutyKind.GOAL, min_interval_s=30, max_interval_s=600)
        plan = (PlanItem("Read new mail", PlanStatus.DOING, due=1_800_000_000.5, note="hourly"),
                PlanItem("Draft replies"))
        info = await memory.save_duty(conv.id, duty, plan)
        assert (info.duty, tuple(info.plan)) == (duty, plan)
        seeded = await memory.create_conversation(seed_from=conv.id)
        assert seeded.duty is None and seeded.plan == ()
        await memory.aclose()
        reopened = open_memory()
        [stored] = [c for c in await reopened.list_conversations() if c.id == conv.id]
        assert (stored.duty, tuple(stored.plan)) == (duty, plan)
        ended = await reopened.save_duty(conv.id, None, plan)
        assert ended.duty is None and tuple(ended.plan) == plan
        with pytest.raises(KeyError):
            await reopened.save_duty("missing", None, ())

    async def test_archived_conversations_are_hidden_from_the_list(self, memory: Memory):
        kept = await memory.create_conversation(title="kept")
        hidden = await memory.create_conversation(title="hidden")
        assert (await memory.update_conversation(hidden.id, archived=True)).archived
        assert [c.id for c in await memory.list_conversations()] == [kept.id]
        everything = await memory.list_conversations(include_archived=True)
        assert {c.id for c in everything} == {kept.id, hidden.id}
        await memory.update_conversation(hidden.id, archived=False)
        assert len(await memory.list_conversations()) == 2

    async def test_unknown_conversation_raises_key_error(
        self, memory: Memory, completion: FakeCompletion
    ):
        calls = [
            memory.append("missing", [text_message("tok0001")]),
            memory.history("missing"),
            memory.assemble(context("missing")),
            memory.maintain("missing", completion, context("missing")),
            memory.search("tok0001", conversation_ids=["missing"]),
            memory.inspect("missing"),
            memory.update_conversation("missing", archived=True),
        ]
        for call in calls:
            with pytest.raises(KeyError):
                await call

    # ── Durability ────────────────────────────────────────────────────────

    async def test_append_is_durable_across_reopen(self, open_memory: MemoryFactory):
        memory = open_memory()
        conv = await memory.create_conversation(title="persisted")
        messages = await self.fill(memory, conv.id, tokens(0, 6))
        await memory.aclose()

        reopened = open_memory()
        assert await active(reopened, conv.id) == messages
        assert await reopened.history(conv.id) == messages
        assert [c.id for c in await reopened.list_conversations()] == [conv.id]
        await assert_archived(reopened, conv.id, messages)

    async def test_append_is_durable_without_clean_shutdown(self, open_memory: MemoryFactory):
        memory = open_memory()
        conv = await memory.create_conversation(title="test")
        messages = await self.fill(memory, conv.id, tokens(0, 4))
        # No aclose(): a second process-like instance must already see everything.
        assert await active(open_memory(), conv.id) == messages

    async def test_second_instance_reads_while_first_writes(self, open_memory: MemoryFactory):
        writer, reader = open_memory(), open_memory()
        conv = await writer.create_conversation(title="test")
        await self.fill(writer, conv.id, tokens(0, 4))

        async def write() -> None:
            await self.fill(writer, conv.id, tokens(100, 20))

        async def read() -> None:
            for _ in range(20):
                await reader.search("tok0001")
                await reader.history(conv.id, limit=3)

        await asyncio.gather(write(), read())
        assert len(await reader.search("tok0118")) == 1

    async def test_all_block_types_round_trip(self, open_memory: MemoryFactory):
        memory = open_memory()
        conv = await memory.create_conversation(title="test")
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
        conv = await memory.create_conversation(title="test")
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
        conv = await memory.create_conversation(title="test")
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
        conv = await memory.create_conversation(title="test")
        messages = await self.fill(memory, conv.id, tokens(0, 30))
        assert await self.maintain(memory, conv.id, completion)
        assert await memory.history(conv.id) == messages  # regardless of compaction
        assert await memory.history(conv.id, limit=3) == messages[-3:]
        assert await memory.history(conv.id, before=messages[10].id) == messages[:10]
        assert await memory.history(conv.id, before=messages[10].id, limit=4) == messages[6:10]
        assert await memory.history(conv.id, before=messages[0].id) == []
        assert await memory.history(conv.id, limit=0) == []
        assert await memory.history(conv.id, after=messages[10].id) == messages[11:]
        assert await memory.history(conv.id, after=messages[10].id, limit=3) == messages[11:14]
        assert await memory.history(conv.id, after=messages[-1].id) == []
        assert await memory.history(conv.id, after=messages[3].id,
                                    before=messages[8].id) == messages[4:8]
        with pytest.raises(KeyError):
            await memory.history(conv.id, after="no-such-message")

    # ── Assemble ──────────────────────────────────────────────────────────

    @pytest.mark.parametrize("budget", [0, 40, 150, 600, 2_000, UNLIMITED])
    @pytest.mark.parametrize("maintained", [False, True])
    async def test_assemble_fits_budget(
        self, memory: Memory, completion: FakeCompletion, budget: int, maintained: bool
    ):
        conv = await memory.create_conversation(title="test")
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
        conv = await memory.create_conversation(title="test")
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
        conv = await memory.create_conversation(title="test")
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
        conv = await memory.create_conversation(title="test")
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
        conv = await memory.create_conversation(title="test")
        messages = await self.fill(memory, conv.id, tokens(0, 2))
        assert await self.maintain(memory, conv.id, completion) is False
        assert await active(memory, conv.id) == messages  # nothing compacted

    async def test_maintain_threshold_follows_the_window(
        self, memory: Memory, completion: FakeCompletion
    ):
        conv = await memory.create_conversation(title="test")
        await self.fill(memory, conv.id, tokens(0, 30))
        assert not await memory.maintain(conv.id, completion, context(conv.id, 100 * WINDOW))
        assert completion.calls == []
        assert await memory.maintain(conv.id, completion, context(conv.id, WINDOW))

    async def test_maintain_compacts_and_archives_first(
        self, memory: Memory, completion: FakeCompletion
    ):
        conv = await memory.create_conversation(title="test")
        messages = await self.fill(memory, conv.id, tokens(0, 30))
        assert await self.maintain(memory, conv.id, completion) is True

        tail = await active(memory, conv.id)
        assert completion.calls, "maintain should have compacted"
        assert completion.calls[0]["compacting"] is True  # frontends say so
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
        conv = await memory.create_conversation(title="test")
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
        conv = await memory.create_conversation(title="test")
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
        conv = await memory.create_conversation(title="test")
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
        conv = await memory.create_conversation(title="test")
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

    # ── Search scope ──────────────────────────────────────────────────────

    async def test_conversations_are_separate_and_search_takes_a_scope(
        self, memory: Memory, completion: FakeCompletion
    ):
        first = await memory.create_conversation(title="test")
        second = await memory.create_conversation(title="test")
        await self.fill(memory, first.id, tokens(0, 30))
        await self.fill(memory, second.id, tokens(500, 30))
        before = await snapshot(memory, second.id)

        await self.maintain(memory, first.id, completion)
        after = await snapshot(memory, second.id)  # long-term memory is shared
        assert after[1] == before[1]
        assert (after[2] and after[2].summary) == (before[2] and before[2].summary)
        assert not set(TOKEN_RE.findall(before[0])) & set(tokens(0, 30))

        first_hits = await memory.search("tok0003", conversation_ids=[first.id])
        assert {h.conversation_id for h in first_hits} == {first.id}
        assert await memory.search("tok0003", conversation_ids=[second.id]) == []
        assert await memory.search("tok0503", conversation_ids=[]) == []
        both = await memory.search("tok0003 tok0503", limit=10)
        assert {h.conversation_id for h in both} == {first.id, second.id}
        scoped = await memory.search("tok0003 tok0503", limit=10,
                                     conversation_ids=[first.id, second.id])
        assert {h.conversation_id for h in scoped} == {first.id, second.id}

    async def test_search_is_read_only(self, memory: Memory, completion: FakeCompletion):
        empty = await memory.create_conversation(title="test")
        conv = await memory.create_conversation(title="test")
        await self.fill(memory, conv.id, tokens(0, 30))
        await self.maintain(memory, conv.id, completion)
        before = await snapshot(memory, conv.id)
        listed = await memory.list_conversations()

        for query in ("tok0001", "fox committee", "", "\"unbalanced ( * OR", "a:b NEAR(c)"):
            await memory.search(query)
        assert await snapshot(memory, conv.id) == before
        assert await memory.list_conversations() == listed
        assert await active(memory, empty.id) == []

    async def test_search_respects_limit(self, memory: Memory):
        conv = await memory.create_conversation(title="test")
        await self.fill(memory, conv.id, tokens(0, 10))
        assert len(await memory.search("fox", limit=3)) == 3
        assert await memory.search("nonexistentword") == []

    async def test_search_higher_score_is_better(self, memory: Memory):
        conv = await memory.create_conversation(title="test")
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
        conv = await memory.create_conversation(title="test")
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
        a = await memory.create_conversation(title="test")
        b = await memory.create_conversation(title="test")
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
        conv = await memory.create_conversation(title="test")
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

    # ── Long-term memory the user can change ──────────────────────────────

    async def editable(self, memory: Memory) -> None:
        try:
            await memory.notes()
        except NotImplementedError:
            pytest.skip("editing long-term memory is optional and not implemented")

    async def test_notes_round_trip(self, open_memory: MemoryFactory, tmp_path: Path):
        """What every Memory that keeps notes does: write, list, rename, delete, durably."""
        memory = open_memory()
        await self.editable(memory)
        project = str(tmp_path / "project")
        note = await memory.save_note("trip", "The Oslo trip", "Leaves Friday.",
                                      folder=project)
        await memory.aclose()
        memory = open_memory()
        [kept] = [n for n in await memory.notes(folder=project) if n.id == note.id]
        assert kept.description == "The Oslo trip" and "Leaves Friday." in kept.text
        renamed = await memory.save_note("journey", "", "Leaves Saturday.", note_id=note.id,
                                         folder=project)
        titles = [n.title for n in await memory.notes(folder=project)]
        assert any(t.startswith("journey") for t in titles)
        assert not any(t.startswith("trip") for t in titles)
        assert "Leaves Saturday." in renamed.text
        await memory.delete_note(renamed.id, folder=project)
        assert not any(n.title.startswith("journey") for n in await memory.notes(folder=project))
        with pytest.raises(KeyError):
            await memory.delete_note(renamed.id, folder=project)

    # ── Continue from ─────────────────────────────────────────────────────

    async def test_seeded_conversation_starts_from_the_source(
        self, memory: Memory, completion: FakeCompletion
    ):
        source = await memory.create_conversation(title="source")
        await self.fill(memory, source.id, tokens(0, 30))
        await self.maintain(memory, source.id, completion)
        before = await snapshot(memory, source.id)
        seeded = await memory.create_conversation(title="seeded", seed_from=source.id)
        assert "tok0029" not in await addendum(memory, seeded.id)
        await memory.append(seeded.id, [text_message("tok0900")])

        assert await self.maintain(memory, seeded.id, completion)
        text = await addendum(memory, seeded.id)
        assert "tok0029" in text  # the source's newest message reached the summary
        assert await snapshot(memory, source.id) == before
        assert len(await memory.history(seeded.id)) == 1  # its own archive starts empty
        assert not await self.maintain(memory, seeded.id, completion)  # seeded once

    # ── Titles ────────────────────────────────────────────────────────────

    async def test_title_is_written_once_from_the_first_user_message(self, memory: Memory):
        conv = await memory.create_conversation()
        titler = FakeCompletion(render=lambda _kept, _text: "Trip plans")
        assert not await self.maintain(memory, conv.id, titler)  # no user message yet
        assert titler.calls == []
        await memory.append(conv.id, [text_message("tok0001")])
        assert await self.maintain(memory, conv.id, titler)
        assert [c["light"] for c in titler.calls] == [True]
        assert titler.calls[0]["max_tokens"] <= 20
        [listed] = await memory.list_conversations()
        assert listed.title == "Trip plans"
        assert not await self.maintain(memory, conv.id, titler)
        assert len(titler.calls) == 1

    async def test_failed_or_empty_title_changes_nothing(self, memory: Memory):
        conv = await memory.create_conversation()
        await memory.append(conv.id, [text_message("tok0001")])
        assert not await self.maintain(memory, conv.id, FakeCompletion(render=lambda *_: ""))
        assert not await self.maintain(memory, conv.id, Failing(CoreError("down")))
        [listed] = await memory.list_conversations()
        assert listed.title is None
        with pytest.raises(BudgetExhausted):
            await self.maintain(memory, conv.id, Failing(BudgetExhausted("spent")))
        assert (await memory.list_conversations())[0].title is None

    async def test_a_set_title_is_never_overwritten(self, memory: Memory):
        conv = await memory.create_conversation()
        await memory.append(conv.id, [text_message("tok0001")])
        await memory.update_conversation(conv.id, title="Mine")
        titler = FakeCompletion(render=lambda *_: "Theirs")
        await self.maintain(memory, conv.id, titler)
        assert titler.calls == []
        assert (await memory.list_conversations())[0].title == "Mine"

    # ── Deletion ──────────────────────────────────────────────────────────

    async def test_delete_conversation(self, open_memory: MemoryFactory,
                                       completion: FakeCompletion):
        memory = open_memory()
        kept = await memory.create_conversation(title="kept")
        doomed = await memory.create_conversation(title="doomed")
        await self.fill(memory, kept.id, tokens(0, 4))
        await self.fill(memory, doomed.id, tokens(100, 30))
        await self.maintain(memory, doomed.id, completion)
        seeded = await memory.create_conversation(title="seeded", seed_from=doomed.id)
        try:
            await memory.delete_conversation(doomed.id)
        except NotImplementedError:
            pytest.skip("deleting conversations is optional and not implemented")
        assert [c.id for c in await memory.list_conversations(include_archived=True)] == [
            seeded.id, kept.id]
        [listed] = [c for c in await memory.list_conversations() if c.id == seeded.id]
        assert listed.seed_from is None
        assert await memory.search("tok0101 tok0120") == []
        assert len(await memory.search("tok0001")) == 1
        for call in (memory.history(doomed.id), memory.assemble(context(doomed.id)),
                     memory.delete_conversation(doomed.id)):
            with pytest.raises(KeyError):
                await call
        await memory.aclose()
        assert (await open_memory().list_conversations())[-1].id == kept.id

