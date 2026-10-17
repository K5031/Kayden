import json
from typing import Callable, Iterator

from kayden.events import (
    ActionEvent,
    CoreReasoningDelta,
    CoreTextDelta,
    CoreToolCall,
    CoreUsage,
    TextDelta,
    ToolFinished,
    ToolResult,
    ToolStarted,
    TurnRequest,
    UsageReported,
)
from kayden.interfaces import ActionInterface, CoreInterface, ToolRuntimeInterface
from kayden.klogger import get_logger


class Module(ActionInterface):
    """Use the core's native function-calling protocol to run tools."""

    def __init__(self, max_rounds: int = 8):
        self.max_rounds = max_rounds
        self.log = get_logger("Action")

    def run(
        self,
        core: CoreInterface,
        turn: TurnRequest,
        tools: ToolRuntimeInterface,
        should_interrupt: Callable[[], bool] | None = None,
    ) -> Iterator[ActionEvent]:
        messages = list(turn.messages)
        schemas = tools.schemas()

        for round_number in range(self.max_rounds + 1):
            if should_interrupt and should_interrupt():
                core.interrupt()
                return

            content = ""
            reasoning = ""
            calls = []
            for event in core.stream(
                messages,
                tools=schemas or None,
                should_interrupt=should_interrupt,
            ):
                if isinstance(event, CoreTextDelta):
                    content += event.text
                    yield TextDelta(event.text)
                elif isinstance(event, CoreReasoningDelta):
                    reasoning += event.text
                elif isinstance(event, CoreToolCall):
                    calls.append(event)
                elif isinstance(event, CoreUsage):
                    yield UsageReported(event.usage)

            if should_interrupt and should_interrupt():
                return
            if not calls:
                return
            if round_number == self.max_rounds:
                yield TextDelta(" [tool limit reached]")
                return

            tool_calls = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in calls
            ]
            assistant_message = {
                "role": "assistant",
                "content": content or None,
                "tool_calls": tool_calls,
            }
            if reasoning:
                assistant_message["reasoning_content"] = reasoning
            messages.append(assistant_message)

            for call in calls:
                if should_interrupt and should_interrupt():
                    core.interrupt()
                    return
                try:
                    arguments = json.loads(call.arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("tool arguments must be a JSON object")
                except (json.JSONDecodeError, ValueError, TypeError) as error:
                    result = ToolResult(f"Tool error: {error}", is_error=True)
                else:
                    yield ToolStarted(call.call_id, call.name, arguments)
                    result = tools.execute(call.name, arguments)

                yield ToolFinished(call.call_id, call.name, result)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "content": result.content,
                })
