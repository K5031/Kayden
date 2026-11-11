"""Skills: instructions for one kind of task, each a folder with a SKILL.md in Claude Code's
format (YAML frontmatter with `name` and `description`, then the instructions), plus any
files the instructions point to. Every request lists them by name and description; the
model loads one with the skill tool (api.SKILL_TOOL) and follows it with its other tools."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from kayden.api import Skill

SKILL_FILE = "SKILL.md"
BUILTIN = Path(__file__).parent.parent / "skills"  # the skills Kayden comes with
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_DESCRIPTION_CHARS = 500  # of a description in the listing
_ARGUMENTS = "$ARGUMENTS"

PROMPT = (
    "# Skills\n"
    "A skill is a set of instructions for one kind of task. When a task fits one of these, "
    "load it with the skill tool before you start, and follow it. When the user's message "
    "starts with /name, load the skill of that name; the rest of the message is its "
    "arguments."
)


@dataclass(frozen=True, slots=True)
class Loaded:
    skill: Skill
    text: str  # the instructions
    folder: Path


def folders(data_dir: Path, project: str | None) -> list[Path]:
    """Where skills are found, a later folder's replacing an earlier one's of the same name:
    Kayden's own, the user's, the project's."""
    found = [BUILTIN, data_dir / "skills"]
    if project is not None:
        found.append(Path(project) / ".claude" / "skills")
    return found


def load(roots: Sequence[Path]) -> tuple[dict[str, Loaded], list[str]]:
    """The skills by name, sorted, and one line per SKILL.md that was skipped. Blocking."""
    found: dict[str, Loaded] = {}
    problems: list[str] = []
    for root in roots:
        try:
            files = sorted(root.glob(f"*/{SKILL_FILE}"))
        except OSError as e:
            problems.append(f"{root}: {e}")
            continue
        for file in files:
            try:
                loaded = _read(file)
            except (OSError, ValueError, yaml.YAMLError) as e:
                problems.append(f"{file}: {' '.join(str(e).split())}")
                continue
            found[loaded.skill.name] = loaded
    return dict(sorted(found.items())), problems


def _read(file: Path) -> Loaded:
    fields, body = _split(file.read_text(encoding="utf-8"))
    name = str(fields.get("name") or file.parent.name)
    if not _NAME.fullmatch(name):
        raise ValueError(f"the name {name!r} is not lowercase letters, digits and hyphens")
    if not body:
        raise ValueError("no instructions")
    description = " ".join(str(fields.get("description") or body.split("\n\n")[0]).split())
    return Loaded(Skill(name, description[:_DESCRIPTION_CHARS]), body, file.parent)


def _split(text: str) -> tuple[Mapping[str, object], str]:
    if not text.startswith("---"):
        return {}, text.strip()
    _, _, rest = text.partition("\n")
    front, closed, body = rest.partition("\n---")
    if not closed:
        raise ValueError("the frontmatter is not closed")
    fields = yaml.safe_load(front) or {}
    if not isinstance(fields, dict):
        raise ValueError("the frontmatter is not a set of fields")  # noqa: TRY004
    return fields, body.partition("\n")[2].strip()


def prompt(skills: Mapping[str, Loaded]) -> str:
    """The system prompt's section; empty without skills."""
    if not skills:
        return ""
    return "\n".join([PROMPT, *(f"- {s.skill.name}: {s.skill.description}"
                                for s in skills.values())])


def render(loaded: Loaded, arguments: str) -> str:
    """What the skill tool answers."""
    text = loaded.text
    if _ARGUMENTS in text:
        text = text.replace(_ARGUMENTS, arguments)
    elif arguments:
        text = f"{text}\n\nArguments: {arguments}"
    return (f"Skill {loaded.skill.name}. Follow these instructions now. Files they name are "
            f"in {loaded.folder}.\n\n{text}")
