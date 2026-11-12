"""A minimal Tools module: one tool the agent can call.

Use it with `[tools] factory = "examples.dice_tools:factory"` in config.toml.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Any

from kayden.api import InstancePaths, ToolCall, ToolContext, ToolOutput, Tools, ToolSpec

ROLL = ToolSpec(
    name="roll_dice",
    description="Roll dice and return each result.",
    parameters={  # JSON Schema; the runtime checks a call against it before execute()
        "type": "object",
        "properties": {
            "sides": {"type": "integer", "minimum": 2},
            "count": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["sides"],
    },
    read_only=True,  # changes nothing, so several calls may run at once
)


class DiceTools(Tools):
    async def list_tools(self) -> Sequence[ToolSpec]:
        return [ROLL]

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolOutput:
        if call.name != ROLL.name:
            return ToolOutput(f"Unknown tool: {call.name}", is_error=True)
        sides, count = call.arguments["sides"], call.arguments.get("count", 1)
        rolls = [random.randint(1, sides) for _ in range(count)]
        return ToolOutput(f"Rolled {rolls}, total {sum(rolls)}.")


def factory(options: dict[str, Any], paths: InstancePaths) -> DiceTools:
    """`options` is the module's table in config.toml; `paths.data_dir` is where an
    instance keeps its files."""
    return DiceTools()


factory.display_name = "Dice"  # type: ignore[attr-defined]  # shown in the settings
