"""Skills: folders of instructions the runtime lists in every request and the model loads
with the skill tool (runtime/skills.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kadence.api import Skill, ToolStatus
from kadence.runtime import skills
from tests.fakes.core import ScriptedCore, Step, call


def write(root: Path, name: str, text: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / skills.SKILL_FILE).write_text(text)
    return folder


GREET = "---\nname: greet\ndescription: Greet someone by name\n---\n\nSay hello to $ARGUMENTS.\n"


@pytest.fixture
def no_builtin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(skills, "BUILTIN", tmp_path / "no-skills")


# ── Loading ──────────────────────────────────────────────────────────────


def test_a_skill_is_a_folder_with_a_skill_file(tmp_path: Path):
    folder = write(tmp_path, "greet", GREET)
    (tmp_path / "notes.txt").write_text("not a skill")
    found, problems = skills.load([tmp_path, tmp_path / "missing"])
    assert problems == []
    assert list(found) == ["greet"]
    assert found["greet"].skill == Skill("greet", "Greet someone by name")
    assert found["greet"].text == "Say hello to $ARGUMENTS."
    assert found["greet"].folder == folder


def test_a_later_folder_replaces_a_skill_of_the_same_name(tmp_path: Path):
    write(tmp_path / "a", "greet", GREET)
    write(tmp_path / "a", "other", "---\ndescription: Another\n---\nx")
    write(tmp_path / "b", "greet", "---\nname: greet\ndescription: Mine\n---\nHi.")
    found, _ = skills.load([tmp_path / "a", tmp_path / "b"])
    assert [(s.skill.name, s.skill.description) for s in found.values()] == [
        ("greet", "Mine"), ("other", "Another")]


def test_name_and_description_have_defaults(tmp_path: Path):
    """As in Claude Code: the folder's name, and the first paragraph of the instructions."""
    write(tmp_path, "release-notes", "Write the release notes\nfrom the git log.\n\nSteps: ...")
    found, _ = skills.load([tmp_path])
    assert found["release-notes"].skill == Skill(
        "release-notes", "Write the release notes from the git log.")


def test_yaml_frontmatter_is_read_as_yaml(tmp_path: Path):
    write(tmp_path, "x", "---\nname: x\ndescription: >\n  Folded over\n  two lines\n"
                         "allowed-tools: [Read]\n---\nBody")
    found, _ = skills.load([tmp_path])
    assert found["x"].skill.description == "Folded over two lines"


def test_a_broken_skill_is_skipped_and_reported(tmp_path: Path):
    write(tmp_path, "bad", "---\nname: [unclosed\n---\nBody")
    write(tmp_path, "Bad Name", "---\nname: Bad Name\ndescription: d\n---\nBody")
    write(tmp_path, "empty", "---\nname: empty\ndescription: d\n---\n")
    write(tmp_path, "greet", GREET)
    found, problems = skills.load([tmp_path])
    assert list(found) == ["greet"]
    assert len(problems) == 3 and all(str(tmp_path) in p for p in problems)


def test_kadence_comes_with_a_memory_cleanup_skill():
    found, problems = skills.load([skills.BUILTIN])
    assert problems == []
    assert "consolidate-memory" in found


# ── In the runtime ───────────────────────────────────────────────────────


async def test_every_request_lists_the_skills_and_offers_the_tool(make, no_builtin):
    h = await make(core=ScriptedCore([Step(text="Hi.")], maintenance=Step(text="T")))
    write(h.data_dir / "skills", "greet", GREET)
    await h.start()
    await h.turn("hello")
    request = h.core.requests[-1]
    assert "- greet: Greet someone by name" in request.system
    assert "Say hello" not in request.system
    assert "skill" in {t.name for t in request.tools}
    assert await h.runtime.skills() == (Skill("greet", "Greet someone by name"),)


async def test_without_skills_nothing_is_listed_or_offered(make, no_builtin):
    h = await make(core=ScriptedCore([Step(text="Hi.")], maintenance=Step(text="T")))
    await h.start()
    await h.turn("hello")
    request = h.core.requests[-1]
    assert "kill" not in request.system
    assert "skill" not in {t.name for t in request.tools}
    assert await h.runtime.skills() == ()


async def test_the_tool_answers_with_the_instructions(make, no_builtin):
    h = await make(core=ScriptedCore(
        [Step(calls=[call("skill", {"name": "/greet", "arguments": "Ada"}, id="a"),
                     call("skill", {"name": "nope"}, id="b")]),
         Step(text="Hello Ada.")], maintenance=Step(text="T")))
    folder = write(h.data_dir / "skills", "greet", GREET)
    await h.start()
    await h.turn("/greet Ada")
    results = await h.results()
    assert results["a"].status == ToolStatus.OK
    assert "Say hello to Ada." in results["a"].content
    assert str(folder) in results["a"].content
    assert results["b"].status == ToolStatus.ERROR
    assert "greet" in results["b"].content


async def test_arguments_follow_instructions_that_do_not_place_them(make, no_builtin):
    h = await make(core=ScriptedCore(
        [Step(calls=[call("skill", {"name": "tidy", "arguments": "the docs"}, id="a")]),
         Step(text="Done.")], maintenance=Step(text="T")))
    write(h.data_dir / "skills", "tidy", "---\ndescription: Tidy up\n---\nTidy things.")
    await h.start()
    await h.turn("go")
    content = (await h.results())["a"].content
    assert "Tidy things." in content and "the docs" in content


async def test_a_project_skill_replaces_the_users(make, no_builtin, tmp_path: Path):
    from tests.fakes.tools import FakeTools

    tools = FakeTools()
    tools.folders = []
    h = await make(tools=tools, core=ScriptedCore([Step(text="Hi."), Step(text="Hi.")],
                                                  maintenance=Step(text="T")))
    write(h.data_dir / "skills", "greet", GREET)
    project = tmp_path / "project"
    write(project / ".claude" / "skills", "greet",
          "---\nname: greet\ndescription: The project's greeting\n---\nAhoy.")
    await h.start(folder=str(project))
    await h.turn("hello")
    assert "- greet: The project's greeting" in h.core.requests[-1].system
    await h.start()  # a chat in no folder sees the user's again
    await h.turn("hello")
    assert "- greet: Greet someone by name" in h.core.requests[-1].system


async def test_a_new_skill_shows_up_without_a_restart(make, no_builtin):
    h = await make(core=ScriptedCore([Step(text="Hi."), Step(text="Hi.")],
                                     maintenance=Step(text="T")))
    await h.start()
    await h.turn("hello")
    write(h.data_dir / "skills", "greet", GREET)
    await h.turn("again")
    assert "- greet:" in h.core.requests[-1].system
