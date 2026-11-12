import asyncio
import shutil
from pathlib import Path

import pytest

from kayden.api import InstancePaths, ToolCall
from kayden.tools.shell_files import MAX_OUTPUT_CHARS, ShellFilesTools, factory
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
            env_probe=ToolCall("e", "shell", {"command": "printenv KAYDEN_PERMISSION_CEILING"}),
        )


def ctx(tmp_path: Path, call_id: str = "c") -> RecordingContext:
    return RecordingContext(call_id)


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


async def test_read_file_reads_a_pdf_as_the_text_of_its_pages(tools, workspace, tmp_path):
    from tests.pdf import make_pdf

    (workspace / "report.pdf").write_bytes(make_pdf(
        ["Sales rose in March", "Costs fell in April", "Outlook for May"]))
    whole = await run(tools, tmp_path, "read_file", path="report.pdf")
    assert whole.content.startswith("[PDF, 3 pages]\n[Page 1]\nSales rose in March")
    assert "[Page 3]\nOutlook for May" in whole.content
    assert not whole.attachments  # text is what it costs: the pages come only when asked for
    some = await run(tools, tmp_path, "read_file", path="report.pdf", pages="2-3")
    assert "[Page 1]" not in some.content and "[Page 2]\nCosts fell in April" in some.content
    one = await run(tools, tmp_path, "read_file", path="report.pdf", pages="3")
    assert one.content == "[PDF, 3 pages]\n[Page 3]\nOutlook for May"
    for wrong in ("0", "4", "3-2", "two"):
        out = await run(tools, tmp_path, "read_file", path="report.pdf", pages=wrong)
        assert out.is_error and "pages" in out.content
    (workspace / "broken.pdf").write_bytes(b"%PDF-1.4 not really")
    assert (await run(tools, tmp_path, "read_file", path="broken.pdf")).is_error


async def test_read_file_shows_a_pdfs_pages_when_asked_to(tools, workspace, tmp_path):
    """For a scan, a chart or a layout: the pages themselves, a few at a time."""
    from tests.pdf import make_pdf

    (workspace / "report.pdf").write_bytes(make_pdf(
        ["Sales rose in March", "Costs fell in April", "Outlook for May"]))
    part = await run(tools, tmp_path, "read_file", path="report.pdf", pages="2-3", view=True)
    [document] = part.attachments
    assert part.content == "[PDF, 3 pages; pages 2-3 attached]" and document.pages == 2
    assert document.text == "[Page 2]\nCosts fell in April\n\n[Page 3]\nOutlook for May"
    whole = await run(tools, tmp_path, "read_file", path="report.pdf", view=True)
    assert whole.content == "[PDF, 3 pages]" and whole.attachments[0].pages == 3
    assert whole.attachments[0].data != document.data  # only the pages asked for
    (workspace / "long.pdf").write_bytes(make_pdf([f"Page text {n}" for n in range(1, 46)]))
    first = await run(tools, tmp_path, "read_file", path="long.pdf", view=True)
    assert first.attachments[0].pages == 20
    assert first.content == '[PDF, 45 pages; pages 1-20 attached; use pages "21-40" to read on]'


async def test_read_file_says_when_a_pdf_has_no_text_to_read(tools, workspace, tmp_path):
    from tests.pdf import make_pdf

    (workspace / "scan.pdf").write_bytes(make_pdf(["", ""]))
    out = await run(tools, tmp_path, "read_file", path="scan.pdf")
    assert "No text could be read" in out.content and "view" in out.content


async def test_read_file_shows_a_picture(tools, workspace, tmp_path):
    from tests.test_attachments import PNG

    (workspace / "shot.png").write_bytes(PNG)
    out = await run(tools, tmp_path, "read_file", path="shot.png")
    assert out.content == "[Image shot.png]" and out.attachments[0].media_type == "image/png"
    (workspace / "huge.png").write_bytes(b"\x89PNG" + b"\x00" * 5 * 1024 * 1024)
    huge = await run(tools, tmp_path, "read_file", path="huge.png")
    assert huge.is_error and "too large" in huge.content


async def test_read_file_offset_and_limit(tools, workspace, tmp_path):
    (workspace / "lines.txt").write_text("".join(f"{i}\n" for i in range(1, 11)))
    out = await run(tools, tmp_path, "read_file", path="lines.txt", offset=3, limit=2)
    assert out.content.startswith("3\n4\n")
    assert "use offset 5 to read on" in out.content


async def test_read_file_stops_at_its_limits_without_reading_everything(tools, workspace,
                                                                         tmp_path):
    (workspace / "zero").symlink_to("/dev/zero")  # endless, with no line breaks
    out = await asyncio.wait_for(run(tools, tmp_path, "read_file", path="zero"), 5)
    assert "[truncated at" in out.content


def outside_secret(tmp_path: Path) -> Path:
    secret = tmp_path / "outside" / "id_key"
    secret.parent.mkdir()
    secret.write_text("PRIVATE KEY\n")
    return secret


async def test_search_skips_links_and_special_files(tools, workspace, tmp_path):
    (workspace / "key.txt").symlink_to(outside_secret(tmp_path))
    (workspace / "zero").symlink_to("/dev/zero")
    out = await asyncio.wait_for(run(tools, tmp_path, "search_files", pattern="."), 5)
    assert "PRIVATE KEY" not in out.content and "key.txt" not in out.content


@pytest.mark.parametrize("name, args", [
    ("read_file", {}),
    ("write_file", {"content": "pwned"}),
    ("edit_file", {"old_string": "PRIVATE", "new_string": "pwned"}),
])
async def test_a_path_swapped_for_a_link_after_the_check_is_refused(tools, workspace, tmp_path,
                                                                    name, args):
    secret = outside_secret(tmp_path)
    call = ToolCall("c", name, {"path": "notes.txt", **args})
    (workspace / "notes.txt").write_text("PRIVATE notes\n")
    assert await tools.confined(call)
    (workspace / "notes.txt").unlink()
    (workspace / "notes.txt").symlink_to(secret)  # swapped between the check and the run
    out = await tools.execute(call, ctx(tmp_path))
    assert out.is_error and "outside the work folder" in out.content
    assert secret.read_text() == "PRIVATE KEY\n" and "PRIVATE KEY" not in out.content


async def test_a_call_that_was_not_confined_may_still_reach_outside(tools, tmp_path):
    secret = outside_secret(tmp_path)  # the user was asked and allowed it
    out = await run(tools, tmp_path, "read_file", path=str(secret))
    assert out.content == "PRIVATE KEY\n"


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


# ── confined to the work folder ──

BWRAP = pytest.mark.skipif(shutil.which("bwrap") is None, reason="needs bubblewrap")


async def confined(tools: ShellFilesTools, name: str, **args) -> bool:
    return await tools.confined(ToolCall("c", name, args))


async def test_file_calls_are_confined_by_path(tools, workspace, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    (workspace / "link").symlink_to(outside)
    assert await confined(tools, "read_file", path="hello.txt")
    assert await confined(tools, "read_file", path=str(workspace / "hello.txt"))
    assert await confined(tools, "write_file", path="new/dir/file.txt", content="x")
    assert await confined(tools, "list_files")
    assert await confined(tools, "search_files", pattern="x", path="pkg")
    assert not await confined(tools, "read_file", path=str(outside))
    assert not await confined(tools, "read_file", path="../outside.txt")
    assert not await confined(tools, "read_file", path="link")  # a symlink leading out
    assert not await confined(tools, "list_files", path=str(tmp_path))
    assert not await confined(tools, "read_file", path=3)
    assert not await confined(tools, "nope")


@BWRAP
async def test_a_sandboxed_command_cannot_leave_the_workspace(tools, workspace, tmp_path):
    assert await confined(tools, "shell", command="true")
    assert not await confined(tools, "shell", command="true", unsandboxed=True)
    outside = tmp_path / "outside.txt"
    out = await run(tools, tmp_path, "shell", command=(
        f"cat hello.txt; echo in > inside.txt; echo out > {outside}; "
        'echo "home:$(ls -A ~ | wc -l)"; test -e /run/user/$(id -u)/bus && echo BUS; true'))
    assert "hello" in out.content and "home:0" in out.content and "BUS" not in out.content
    assert (workspace / "inside.txt").read_text() == "in\n"
    assert not outside.exists()

    out = await run(tools, tmp_path, "shell", command=f"echo out > {outside}", unsandboxed=True)
    assert not out.is_error and outside.read_text() == "out\n"


async def test_without_a_sandbox_the_shell_is_not_confined(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    tools = ShellFilesTools(workspace)
    assert not await confined(tools, "shell", command="true")
    assert await confined(tools, "read_file", path="hello.txt")
    outside = tmp_path / "outside.txt"
    out = await run(tools, tmp_path, "shell", command=f"echo out > {outside}")
    assert not out.is_error and outside.exists()  # runs as before, after the user allowed it


async def test_a_work_folder_that_holds_the_home_folder_confines_nothing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "project").mkdir(parents=True)
    (home / "notes.txt").write_text("x")
    monkeypatch.setenv("HOME", str(home))
    for root in (home, tmp_path):  # the home folder itself, and a folder that holds it
        tools = ShellFilesTools(root)
        assert not await confined(tools, "read_file", path=str(home / "notes.txt"))
        assert not await confined(tools, "shell", command="true")
    assert await confined(ShellFilesTools(home / "project"), "list_files")


async def test_read_only_flags(tools):
    flags = {spec.name: spec.read_only for spec in await tools.list_tools()}
    assert flags == {
        "shell": False,
        "read_file": True,
        "write_file": False,
        "edit_file": False,
        "list_files": True,
        "search_files": True,
        "web_search": True,
        "web_fetch": True,
    }
    stall = {spec.name: spec.stall_timeout for spec in await tools.list_tools()}
    assert stall["shell"] and all(v is None for k, v in stall.items() if k != "shell")


async def test_file_walks_send_heartbeats_from_their_thread(tools, tmp_path):
    for name, args in (("search_files", {"pattern": "answer"}), ("list_files", {})):
        c = ctx(tmp_path)
        out = await tools.execute(ToolCall("c", name, args), c)
        assert not out.is_error
        assert c.heartbeats >= 1 and c.foreign_thread_calls >= 1 and c.reports == []


async def test_timeouts_are_configurable(workspace):
    import math

    specs = {s.name: s for s in await ShellFilesTools(workspace).list_tools()}
    assert specs["shell"].timeout is None and specs["shell"].stall_timeout == 600
    tools = ShellFilesTools(workspace, shell_timeout=math.inf, shell_stall_timeout=None)
    shell = {s.name: s for s in await tools.list_tools()}["shell"]
    assert shell.timeout == math.inf and shell.stall_timeout is None


async def test_factory(workspace, tmp_path, monkeypatch):
    tools = factory({"root": str(workspace), "shell_timeout": 60}, InstancePaths(tmp_path))
    assert isinstance(tools, ShellFilesTools) and tools.root == workspace.resolve()
    monkeypatch.chdir(workspace / "pkg")
    assert factory({}, InstancePaths(tmp_path)).root == (workspace / "pkg").resolve()


async def test_use_folder_moves_every_tool_to_that_folder(tools, workspace, tmp_path):
    other = tmp_path / "other" / "project"
    await tools.use_folder(other)  # created when missing
    assert other.is_dir() and tools.root == other.resolve()
    await run(tools, tmp_path, "write_file", path="notes.txt", content="here")
    assert (other / "notes.txt").read_text() == "here"
    assert await confined(tools, "read_file", path="notes.txt")
    assert not await confined(tools, "read_file", path=str(workspace / "hello.txt"))
    out = await run(tools, tmp_path, "shell", command="pwd")
    assert out.content.startswith(str(other.resolve()))
    await tools.use_folder(None)  # back to the folder the module was built with
    assert tools.root == workspace.resolve()
    assert (await run(tools, tmp_path, "read_file", path="hello.txt")).content == "hello\nworld\n"


@BWRAP
async def test_the_sandbox_follows_the_folder(tools, workspace, tmp_path):
    other = tmp_path / "other"
    await tools.use_folder(other)
    assert await confined(tools, "shell", command="true")
    out = await run(tools, tmp_path, "shell", command=(
        f"echo in > inside.txt; echo out > {workspace / 'escaped.txt'}; true"))
    assert not out.is_error
    assert (other / "inside.txt").exists() and not (workspace / "escaped.txt").exists()


async def test_use_folder_on_a_folder_that_holds_home_confines_nothing(tools, tmp_path,
                                                                       monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    await tools.use_folder(home)
    assert not await confined(tools, "read_file", path="x.txt")
    assert not await confined(tools, "shell", command="true")


async def test_use_folder_fails_for_a_path_that_cannot_be_a_folder(tools, workspace):
    with pytest.raises(OSError):
        await tools.use_folder(workspace / "hello.txt" / "sub")
    assert tools.root == workspace.resolve()


def test_secret_variables_are_left_out_of_the_sandbox():
    from kayden.tools.shell_files import sandbox_environment

    env = sandbox_environment({
        "ANTHROPIC_API_KEY": "k", "DEEPSEEK_API_KEY": "k", "GITHUB_TOKEN": "t",
        "AWS_SECRET_ACCESS_KEY": "s", "DB_PASSWORD": "p", "MY_CREDENTIALS": "c",
        "PATH": "/usr/bin", "HOME": "/home/u", "LANG": "C.UTF-8",
        "KAYDEN_PERMISSION_CEILING": "auto", "ANTHROPIC_BASE_URL": "https://x",
    })
    assert env == {"PATH": "/usr/bin", "HOME": "/home/u", "LANG": "C.UTF-8",
                   "KAYDEN_PERMISSION_CEILING": "auto", "ANTHROPIC_BASE_URL": "https://x"}


@BWRAP
async def test_a_sandboxed_command_cannot_read_keys_from_the_environment(
        tools, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("KAYDEN_PERMISSION_CEILING", "auto")
    probe = 'echo "key=${ANTHROPIC_API_KEY:-none} ceiling=$KAYDEN_PERMISSION_CEILING"'
    out = await run(tools, tmp_path, "shell", command=probe)
    assert "key=none ceiling=auto" in out.content
    out = await run(tools, tmp_path, "shell", command=probe, unsandboxed=True)
    assert "key=sk-secret" in out.content  # asked for first, so it runs as the user would


async def test_no_tool_declares_its_own_description(tools):
    """The runtime adds the description to every tool (api.DESCRIPTION_ARG)."""
    from kayden.api import DESCRIPTION_ARG

    for spec in await tools.list_tools():
        assert DESCRIPTION_ARG not in spec.parameters["properties"], spec.name


async def test_a_new_folder_forgets_calls_judged_for_the_old_one(tools, workspace, tmp_path):
    call = ToolCall("c", "read_file", {"path": "notes.txt"})
    assert await tools.confined(call)  # judged, then never run (say, denied)
    await tools.use_folder(tmp_path / "other")
    assert not tools._confined_calls
