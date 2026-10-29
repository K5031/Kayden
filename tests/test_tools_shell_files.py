import asyncio
from pathlib import Path

import pytest

from kayden.api import ToolCall
from kayden.tools.shell_files import MAX_OUTPUT_CHARS, ShellFilesTools
from tests.conformance.tools_suite import RecordingContext, ToolCases, ToolsConformance

# Two background children plus a ticking loop: cancellation must reach all of them.
LONG_COMMAND = "sleep 30 & sleep 30 & for i in $(seq 1 100); do echo tick $i; sleep 0.1; done"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "pkg").mkdir(parents=True)
    (root / "hello.txt").write_text("hello\nworld\n")
    (root / "pkg" / "mod.py").write_text("def answer():\n    return 42\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("hidden answer\n")
    return root


class TestShellFilesConformance(ToolsConformance):
    @pytest.fixture
    def tools(self, workspace: Path) -> ShellFilesTools:
        return ShellFilesTools(workspace)

    @pytest.fixture
    def cases(self, workspace: Path) -> ToolCases:
        return ToolCases(
            quick=ToolCall("q", "read_file", {"path": "hello.txt"}),
            failing=ToolCall("f", "read_file", {"path": "missing.txt"}),
            long_running=ToolCall("l", "shell", {"command": LONG_COMMAND}),
            extra=(
                ToolCall("s", "search_files", {"pattern": "answer"}),
                ToolCall("ls", "list_files", {}),
                ToolCall("sh", "shell", {"command": "echo hi"}),
            ),
        )


def ctx(tmp_path: Path, call_id: str = "c") -> RecordingContext:
    return RecordingContext(call_id, tmp_path)


async def run(tools: ShellFilesTools, tmp_path: Path, name: str, **args):
    return await tools.execute(ToolCall("c", name, args), ctx(tmp_path))


@pytest.fixture
def tools(workspace: Path) -> ShellFilesTools:
    return ShellFilesTools(workspace)


async def test_shell_success_and_exit_code(tools, tmp_path):
    out = await run(tools, tmp_path, "shell", command="echo out; echo err >&2")
    assert not out.is_error
    assert "out" in out.content and "err" in out.content
    assert out.content.endswith("[exit code 0]")

    out = await run(tools, tmp_path, "shell", command="echo nope; exit 3")
    assert out.is_error and "nope" in out.content and "[exit code 3]" in out.content


async def test_shell_runs_in_workspace(tools, workspace, tmp_path):
    out = await run(tools, tmp_path, "shell", command="pwd")
    assert Path(out.content.splitlines()[0]).resolve() == workspace.resolve()


async def test_shell_truncates_long_output(tools, tmp_path):
    out = await run(tools, tmp_path, "shell", command="seq 1 200000")
    assert len(out.content) < MAX_OUTPUT_CHARS + 200
    assert out.content.startswith("1\n") and "200000" in out.content
    assert "omitted" in out.content


async def test_shell_streams_progress(tools, tmp_path):
    c = ctx(tmp_path)
    await tools.execute(
        ToolCall("c", "shell", {"command": "echo one; sleep 0.3; echo two"}), c
    )
    assert [text for _, text in c.reports] == ["one", "two"]


async def test_shell_does_not_hang_on_background_child(tools, tmp_path):
    # The child inherits stdout; the tool must still return once the shell exits.
    out = await asyncio.wait_for(
        run(tools, tmp_path, "shell", command="sleep 5 & echo started"), 3
    )
    assert "started" in out.content
    await run(tools, tmp_path, "shell", command="pkill -f 'sleep 5' || true")


async def test_read_file_offset_and_limit(tools, workspace, tmp_path):
    (workspace / "lines.txt").write_text("".join(f"{i}\n" for i in range(1, 11)))
    out = await run(tools, tmp_path, "read_file", path="lines.txt", offset=3, limit=2)
    assert out.content.startswith("3\n4\n")
    assert "10 lines total" in out.content


async def test_read_directory_is_error(tools, tmp_path):
    assert (await run(tools, tmp_path, "read_file", path="pkg")).is_error


async def test_write_creates_parents(tools, workspace, tmp_path):
    out = await run(tools, tmp_path, "write_file", path="a/b/c.txt", content="x")
    assert not out.is_error
    assert (workspace / "a/b/c.txt").read_text() == "x"


async def test_edit_file(tools, workspace, tmp_path):
    (workspace / "e.txt").write_text("foo bar foo")
    out = await run(tools, tmp_path, "edit_file", path="e.txt", old_string="foo", new_string="x")
    assert out.is_error and "2 times" in out.content
    out = await run(tools, tmp_path, "edit_file", path="e.txt", old_string="bar", new_string="B")
    assert not out.is_error
    out = await run(
        tools, tmp_path, "edit_file",
        path="e.txt", old_string="foo", new_string="F", replace_all=True,
    )
    assert not out.is_error
    assert (workspace / "e.txt").read_text() == "F B F"
    out = await run(tools, tmp_path, "edit_file", path="e.txt", old_string="zzz", new_string="")
    assert out.is_error and "not found" in out.content


async def test_list_files(tools, tmp_path):
    out = await run(tools, tmp_path, "list_files")
    assert out.content.splitlines() == ["hello.txt", "pkg/mod.py"]
    out = await run(tools, tmp_path, "list_files", pattern="*.py")
    assert out.content == "pkg/mod.py"


async def test_search_files_skips_hidden(tools, tmp_path):
    out = await run(tools, tmp_path, "search_files", pattern="answer")
    assert out.content == "pkg/mod.py:1: def answer():"
    out = await run(tools, tmp_path, "search_files", pattern="(")
    assert out.is_error


async def test_read_only_flags(tools):
    flags = {spec.name: spec.read_only for spec in await tools.list_tools()}
    assert flags == {
        "shell": False,
        "read_file": True,
        "write_file": False,
        "edit_file": False,
        "list_files": True,
        "search_files": True,
    }
    stall = {spec.name: spec.stall_timeout for spec in await tools.list_tools()}
    assert stall["shell"] and all(v is None for k, v in stall.items() if k != "shell")
