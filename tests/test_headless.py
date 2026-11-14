"""The headless frontend: another program drives Kadence with JSON-RPC 2.0 over stdin and
stdout, one message per line, and gets every runtime event as a notification."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest

import kadence.__main__ as launcher
from kadence.api import AgentState, Budget, DutyKind, PermissionPolicy, StartOptions
from kadence.frontends.headless import HeadlessFrontend, factory
from tests.conftest import Harness, eventually
from tests.fakes.core import ScriptedCore, Step, call


class Client:
    """The spawning program's side of the pipes."""

    def __init__(self, frontend_kw: dict[str, Any] | None = None) -> None:
        self.to_frontend_r, self.to_frontend_w = os.pipe()
        self.from_frontend_r, self.from_frontend_w = os.pipe()
        self.frontend = HeadlessFrontend(
            **(frontend_kw or {}),
            stdin=open(self.to_frontend_r, "rb", buffering=0),  # noqa: SIM115
            stdout=open(self.from_frontend_w, "wb", buffering=0))  # noqa: SIM115
        self.replies = open(self.from_frontend_r, "rb", buffering=0)  # noqa: SIM115
        self.messages: list[dict[str, Any]] = []
        self.next_id = 0
        self._reader: asyncio.Task[None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._transport: asyncio.BaseTransport | None = None
        self.stdin_open = True

    async def run(self, runtime: Any) -> asyncio.Task[None]:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        self._transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), self.replies)

        async def read() -> None:
            while line := await reader.readline():
                self.messages.append(json.loads(line))

        self._reader = asyncio.create_task(read())
        self._task = asyncio.create_task(self.frontend.run(runtime))
        return self._task

    def write(self, payload: dict[str, Any] | str) -> None:
        line = payload if isinstance(payload, str) else json.dumps(payload)
        os.write(self.to_frontend_w, (line + "\n").encode())

    async def request(self, method: str, **params: Any) -> dict[str, Any]:
        self.next_id += 1
        request_id = self.next_id
        self.write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return await eventually(lambda: next(
            (m for m in self.messages if m.get("id") == request_id), None))

    def events(self, kind: str | None = None) -> list[dict[str, Any]]:
        return [m["params"]["event"] for m in self.messages if m.get("method") == "event"
                and (kind is None or m["params"]["event"]["type"] == kind)]

    def close_stdin(self) -> None:
        if self.stdin_open:
            self.stdin_open = False
            os.close(self.to_frontend_w)

    async def close(self) -> None:
        self.close_stdin()
        if self._task is not None:
            await asyncio.wait_for(self._task, 5)
        if self._transport is not None:
            self._transport.close()
        if self._reader is not None:
            await asyncio.wait_for(self._reader, 5)


@pytest.fixture
async def clients():
    made: list[Client] = []

    def _make(**kw: Any) -> Client:
        made.append(Client(kw))
        return made[-1]

    yield _make
    for c in made:
        await c.close()


async def test_a_conversation_is_driven_over_the_channel(make, clients):
    h: Harness = await make(core=ScriptedCore([Step(text="Hello from a unit")]))
    client = clients()
    await client.run(h.runtime)
    started = await client.request("start")
    conversation = started["result"]
    assert conversation["type"] == "ConversationInfo" and conversation["duty"] is None
    assert (await client.request("send", text="hi"))["result"] is None
    await eventually(lambda: len(client.events("MessageCommitted")) == 2
                     and client.events("StateChanged")[-1]["state"] == "idle")
    [_, answer] = client.events("MessageCommitted")
    assert answer["message"]["blocks"] == [{"type": "Text", "text": "Hello from a unit"}]
    seqs = [m["params"]["seq"] for m in client.messages if m.get("method") == "event"]
    assert seqs == sorted(seqs)
    history = (await client.request("history", conversation_id=conversation["id"]))["result"]
    assert [m["role"] for m in history] == ["user", "assistant"]
    snapshot = (await client.request("snapshot"))["result"]
    assert snapshot["type"] == "RuntimeSnapshot" and snapshot["state"] == "idle"


async def test_errors_keep_their_message(make, clients):
    h: Harness = await make()
    client = clients()
    await client.run(h.runtime)
    refused = (await client.request("send", text="too early"))["error"]
    assert refused["code"] == -32000 and refused["data"]["type"] == "RuntimeStateError"
    assert "not started" in refused["message"]
    assert (await client.request("fly"))["error"]["code"] == -32601
    assert (await client.request("send", words="x"))["error"]["code"] == -32602
    client.write("not json")
    await eventually(lambda: any(m.get("error", {}).get("code") == -32700
                                 for m in client.messages))


async def test_a_duty_is_set_and_the_plan_is_read(make, clients):
    h: Harness = await make(core=ScriptedCore([Step(text="on it")]),
                            budget=Budget(max_cost_usd=5))
    client = clients()
    await client.run(h.runtime)
    await client.request("start")
    reply = await client.request("set_duty", duty={"instruction": "Answer the inbox",
                                                   "kind": "goal", "max_interval_s": 600})
    assert reply["result"] is None
    await client.request("set_plan", plan=[{"text": "Read mail", "status": "doing"}])
    info = (await client.request("snapshot"))["result"]["conversation"]
    assert info["duty"]["kind"] == "goal" and info["duty"]["max_interval_s"] == 600
    assert info["plan"] == [{"type": "PlanItem", "text": "Read mail", "status": "doing",
                             "due": None, "note": ""}]


async def test_permission_prompts_go_out_and_are_answered(make, clients):
    h: Harness = await make(core=ScriptedCore([Step(calls=[call("write", id="w")]),
                                               Step(text="written")]),
                            permissions=PermissionPolicy.ASK)
    h.tools.instant("write", read_only=False)
    client = clients()
    await client.run(h.runtime)
    await client.request("start")
    await client.request("send", text="write it")
    [asked] = await eventually(lambda: client.events("PermissionRequested"))
    request = asked["request"]
    assert request["call"]["name"] == "write"
    await client.request("resolve_permission", request_id=request["id"],
                         decision="allow_once")
    await eventually(lambda: (h.store.conversations and any(
        getattr(b, "status", None) == "ok" for m in next(iter(
            h.store.conversations.values())).messages for b in m.blocks)))


async def test_closing_stdin_ends_the_frontend(make, clients):
    h: Harness = await make()
    client = clients()
    task = await client.run(h.runtime)
    client.close_stdin()
    await asyncio.wait_for(task, 3)


async def test_a_launch_duty_starts_a_new_conversation_on_it(make, clients):
    h: Harness = await make(core=ScriptedCore([Step(text="working on it")]),
                            budget=Budget(max_cost_usd=5))
    client = clients(start=StartOptions(), duty="Run the socials", kind=DutyKind.DUTY)
    await client.run(h.runtime)
    await eventually(lambda: h.runtime.snapshot().conversation is not None
                     and h.runtime.snapshot().conversation.duty is not None)
    assert h.runtime.snapshot().conversation.duty.instruction == "Run the socials"
    await eventually(lambda: len(h.core.requests) == 1
                     and h.runtime.snapshot().state == AgentState.IDLE)


async def test_a_launch_duty_that_cannot_run_ends_the_unit(make, clients, capsys):
    h: Harness = await make()
    client = clients(start=StartOptions(), duty="  ", kind=DutyKind.DUTY)  # says nothing
    task = await client.run(h.runtime)
    await asyncio.wait_for(task, 3)
    assert "needs an instruction" in capsys.readouterr().err


def test_factory_options():
    frontend = factory({"start": True, "conversation_id": "c1", "duty": "Watch",
                        "goal": True}, None)
    assert frontend.info.interactive_permissions
    assert frontend.start == StartOptions(conversation_id="c1")
    assert (frontend.duty, frontend.kind) == ("Watch", DutyKind.GOAL)


def test_the_launcher_runs_the_headless_frontend(tmp_path):
    args = launcher.parse_args(["--data-dir", str(tmp_path), "--headless", "--goal", "Ship"])
    config = launcher.load_config(args)
    assert config.frontend.factory == "kadence.frontends.headless:factory"
    assert config.frontend.options == {"start": True, "duty": "Ship", "goal": True}
    assert config.runtime.modules.running["frontend"] is None


def test_a_duty_needs_headless(tmp_path):
    with pytest.raises(SystemExit):
        launcher.load_config(launcher.parse_args(["--data-dir", str(tmp_path),
                                                  "--duty", "x"]))


async def test_a_real_process_keeps_stdout_for_the_channel(tmp_path):
    import sys
    from pathlib import Path

    data = tmp_path / "unit"
    data.mkdir()
    (data / "config.toml").write_text(
        '[core]\nfactory = "tests.fakes.core:factory"\n'
        '[tools]\nfactory = "tests.fakes.tools:factory"\n'
        '[runtime.budget]\nmax_cost_usd = 5.0\n')
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root)}
    env.pop("KADENCE_PERMISSION_CEILING", None)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import kadence.__main__ as m; print('stray'); m.main()",
        "--headless", "--data-dir", str(data), "--goal", "Say ok",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=root, env=env)
    lines: list[dict[str, Any]] = []
    try:
        async def until_answered() -> None:
            while line := await process.stdout.readline():
                lines.append(json.loads(line))  # every stdout line is the protocol's
                if any(e["params"]["event"]["type"] == "MessageCommitted"
                       and e["params"]["event"]["message"]["role"] == "assistant"
                       for e in lines if e.get("method") == "event"):
                    return

        await asyncio.wait_for(until_answered(), 20)
    finally:
        process.stdin.close()
        stderr = (await asyncio.wait_for(process.communicate(), 20))[1].decode()
    assert process.returncode == 0, stderr
    assert "stray" in stderr  # printed before the channel was claimed, still kept off it
