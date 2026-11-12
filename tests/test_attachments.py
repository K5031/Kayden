"""Files the user attaches to a message: read by the runtime into Image and File blocks,
stored by Memory, sent by the Cores."""

from __future__ import annotations

import base64
import time
from pathlib import Path

import pytest

from kayden.api import (
    AgentState,
    Document,
    File,
    Image,
    InferenceRequest,
    Message,
    MessageQueued,
    Role,
    Text,
    attachment_text,
)
from kayden.core.openai_compat import to_messages as openai_messages
from kayden.memory.sqlite import SqliteMemory
from kayden.runtime import attachments
from kayden.runtime.budget import estimate_tokens
from tests.conformance.core_suite import collect
from tests.conftest import eventually
from tests.core.test_anthropic import MODEL, AnthropicDriver
from tests.fakes.core import ScriptedCore, Step
from tests.pdf import make_pdf

# A 1x1 transparent PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5E"
    "rkJggg==")


@pytest.fixture
def files(tmp_path: Path) -> dict[str, str]:
    """One of each kind, by a short name."""
    made = {"png": tmp_path / "shot.png", "txt": tmp_path / "notes.txt",
            "bin": tmp_path / "data.bin", "big": tmp_path / "big.log"}
    made["png"].write_bytes(PNG)
    made["txt"].write_text("Buy oat milk.\n")
    made["bin"].write_bytes(b"\x00\x01\x02\xff")
    made["big"].write_text("x" * (attachments.MAX_TEXT_BYTES + 1))
    return {name: str(path) for name, path in made.items()}


# ── Reading them ─────────────────────────────────────────────────────────


def test_an_image_is_read_whole_and_a_text_file_with_its_content(files):
    image, note, binary, big = attachments.read(
        [files["png"], files["txt"], files["bin"], files["big"]])
    assert image == Image("image/png", base64.b64encode(PNG).decode(), "shot.png")
    assert note == File(files["txt"], "Buy oat milk.\n")
    assert binary == File(files["bin"]) and big == File(files["big"])  # named, not included


def test_a_file_that_is_not_there_is_refused(tmp_path: Path):
    with pytest.raises(ValueError, match="cannot attach"):
        attachments.read([str(tmp_path / "gone.txt")])
    with pytest.raises(ValueError, match="not a file"):
        attachments.read([str(tmp_path)])


def test_an_image_too_large_for_a_provider_goes_by_its_path(tmp_path: Path):
    path = tmp_path / "huge.png"
    path.write_bytes(b"\x89PNG" + b"\x00" * attachments.MAX_IMAGE_BYTES)
    assert attachments.read([str(path)]) == (File(str(path)),)


def test_how_an_attachment_reads_as_text():
    assert attachment_text(Image("image/png", "AAAA", "shot.png")) == "[Image attached: shot.png]"
    assert attachment_text(File("/w/notes.txt", "Buy oat milk.")) == (
        "[File attached: /w/notes.txt]\nBuy oat milk.")
    assert "not included" in attachment_text(File("/w/data.bin"))


def test_a_pdf_is_read_whole_with_its_text(tmp_path: Path):
    path = tmp_path / "report.pdf"
    raw = make_pdf(["Sales rose in March", "Costs fell in April"])
    path.write_bytes(raw)
    [document] = attachments.read([str(path)])
    assert document == Document(str(path), base64.b64encode(raw).decode(),
                                "Sales rose in March\n\nCosts fell in April", 2)
    assert attachment_text(document) == (
        f"[File attached: {path} (PDF, 2 pages)]\nSales rose in March\n\nCosts fell in April")
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 not really")
    assert attachments.read([str(broken)]) == (File(str(broken)),)  # by its path alone


def test_a_pdf_counts_its_pages_toward_the_context():
    document = Document("/w/report.pdf", "AAAA", "Sales rose", 3)
    plain = Message("m", Role.USER, (Text("hi"),), 0.0)
    assert estimate_tokens([Message("m", Role.USER, (Text("hi"), document), 0.0)]) \
        >= estimate_tokens([plain]) + 3 * 1000


# ── In the runtime ───────────────────────────────────────────────────────


async def test_a_message_carries_what_was_attached(make, files):
    h = await make(core=ScriptedCore([Step(text="A dot, and a shopping note.")]))
    await h.start()
    seq = h.runtime.snapshot().seq
    await h.runtime.send("what are these?", files=[files["png"], files["txt"]])
    await h.rec.wait_for(lambda r: any(
        e.seq > seq and getattr(e.event, "state", None) == AgentState.IDLE for e in r.envelopes))
    user = (await h.transcript())[0]
    assert [type(b) for b in user.blocks] == [Text, Image, File]
    assert user.blocks[0].text == "what are these?"
    assert user.blocks[2] == File(files["txt"], "Buy oat milk.\n")
    sent = h.core.requests[0].messages[0]
    assert [type(b) for b in sent.blocks] == [Text, Image, File]  # the model gets them


async def test_attachments_alone_are_a_message_and_a_bad_one_sends_nothing(make, files, tmp_path):
    h = await make(core=ScriptedCore([Step(text="A dot.")]))
    await h.start()
    with pytest.raises(ValueError, match="cannot attach"):
        await h.runtime.send("look", files=[files["png"], str(tmp_path / "gone.png")])
    assert not await h.transcript() and h.runtime.snapshot().state == AgentState.IDLE
    await h.runtime.send("", files=[files["png"]])
    await eventually(lambda: len(h.core.requests) == 1
                     and h.runtime.snapshot().state == AgentState.IDLE)
    assert isinstance((await h.transcript())[0].blocks[1], Image)


async def test_a_message_queued_while_the_agent_works_keeps_its_attachments(make, files):
    h = await make(core=ScriptedCore([Step(text="working", hang=True), Step(text="A dot.")]))
    await h.start()
    await h.runtime.send("start")
    await eventually(lambda: len(h.core.requests) == 1)
    await h.runtime.send("and this", files=[files["png"]])
    await h.rec.wait_for(lambda r: r.of(MessageQueued))
    [queued] = h.rec.of(MessageQueued)
    assert isinstance(queued.attachments[0], Image)
    assert h.runtime.snapshot().queued_messages[0].attachments == queued.attachments


def test_an_image_counts_toward_the_context(files):
    [image] = attachments.read([files["png"]])
    plain = Message("m", Role.USER, (Text("hi"),), 0.0)
    assert estimate_tokens([Message("m", Role.USER, (Text("hi"), image), 0.0)]) \
        >= estimate_tokens([plain]) + 1000


# ── To the model ─────────────────────────────────────────────────────────


def _message(*blocks: object) -> Message:
    return Message("u", Role.USER, tuple(blocks), time.time())


async def test_claude_gets_the_image_and_the_files_text():
    anthropic = AnthropicDriver()
    anthropic.queue_text()
    await collect(anthropic.core, InferenceRequest(MODEL, "", [_message(
        Text("what are these?"), Image("image/png", "AAAA", "shot.png"),
        File("/w/notes.txt", "Buy oat milk."))], [], max_output_tokens=256))
    await anthropic.core.aclose()
    [turn] = anthropic.stub.bodies[-1]["messages"]
    assert turn["content"] == [
        {"type": "text", "text": "what are these?"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": "AAAA"}},
        {"type": "text", "text": "[File attached: /w/notes.txt]\nBuy oat milk."}]


async def test_claude_gets_a_pdf_as_a_document_before_the_text():
    anthropic = AnthropicDriver()
    anthropic.queue_text()
    await collect(anthropic.core, InferenceRequest(MODEL, "", [_message(
        Text("summarize this"), Document("/w/report.pdf", "JVBERi0=", "Sales rose", 1))], [],
        max_output_tokens=256))
    await anthropic.core.aclose()
    [turn] = anthropic.stub.bodies[-1]["messages"]
    assert turn["content"] == [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                        "data": "JVBERi0="}},
        {"type": "text", "text": "summarize this"}]


def test_other_providers_get_a_pdfs_text():
    [seen] = openai_messages("", [_message(
        Text("summarize this"), Document("/w/report.pdf", "JVBERi0=", "Sales rose", 1))])
    assert seen["content"] == ("summarize this\n\n"
                               "[File attached: /w/report.pdf (PDF, 1 page)]\nSales rose")


def test_openai_style_providers_get_an_image_part_or_a_line_of_text():
    message = _message(Text("what is this?"), Image("image/png", "AAAA", "shot.png"),
                       File("/w/notes.txt", "Buy oat milk."))
    [seen] = openai_messages("", [message])
    assert seen["content"] == [
        {"type": "text", "text": "what is this?\n\n[File attached: /w/notes.txt]\nBuy oat milk."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    [blind] = openai_messages("", [message], images=False)  # a provider without vision
    assert blind["content"] == ("what is this?\n\n[File attached: /w/notes.txt]\nBuy oat milk."
                                "\n\n[Image attached: shot.png]")
    [only] = openai_messages("", [_message(Image("image/png", "AAAA"))])
    assert only["content"] == [{"type": "image_url",
                                "image_url": {"url": "data:image/png;base64,AAAA"}}]


# ── In memory ────────────────────────────────────────────────────────────


async def test_memory_keeps_attachments_and_searches_a_files_text_not_image_data(tmp_path):
    memory = SqliteMemory(tmp_path / "m.db")
    conversation = await memory.create_conversation()
    image = Image("image/png", "QUJDREVGR0g", "shot.png")
    note = File("/w/notes.txt", "Buy oat milk.")
    await memory.append(conversation.id, [_message(Text("look"), image, note)])
    [stored] = await memory.history(conversation.id)
    assert stored.blocks == (Text("look"), image, note)
    assert await memory.search("oat", conversation_ids=[conversation.id])
    assert not await memory.search("QUJDREVGR0g", conversation_ids=[conversation.id])
    pdf = Document("/w/report.pdf", "SlZCRVJpMA", "Sales rose in March", 1)
    await memory.append(conversation.id, [Message("p", Role.USER, (pdf,), time.time())])
    assert (await memory.history(conversation.id))[-1].blocks == (pdf,)
    assert await memory.search("March", conversation_ids=[conversation.id])
    assert not await memory.search("SlZCRVJpMA", conversation_ids=[conversation.id])
    await memory.aclose()
