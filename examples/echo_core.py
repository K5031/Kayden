"""A minimal Core: a "model" that repeats what the user said, with no network.

Use it with `[core] factory = "examples.echo_core:factory"` in config.toml; it lets
Kayden run without an API key.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

from kayden.api import (
    Completed,
    Core,
    CoreEvent,
    InferenceRequest,
    InstancePaths,
    Message,
    ModelInfo,
    Role,
    StopReason,
    Text,
    TextDelta,
    Usage,
)

MODEL = ModelInfo("echo-1", "Echo", context_window=100_000, max_output_tokens=4_000,
                  input_usd_per_mtok=0.0, output_usd_per_mtok=0.0)


class EchoCore(Core):
    async def list_models(self) -> Sequence[ModelInfo]:
        return [MODEL]

    async def stream(self, request: InferenceRequest) -> AsyncIterator[CoreEvent]:
        """Deltas as the answer arrives, then always Completed with the whole message."""
        said = next((block.text for message in reversed(request.messages)
                     if message.role is Role.USER
                     for block in message.blocks if isinstance(block, Text)), "")
        answer = f"You said: {said}"
        for word in answer.split(" "):
            yield TextDelta(word + " ")
        message = Message(uuid.uuid4().hex, Role.ASSISTANT, (Text(answer),), 0.0)
        usage = Usage(input_tokens=len(said) // 4, output_tokens=len(answer) // 4)
        yield Completed(message, usage, StopReason.END_TURN)


def factory(options: dict[str, Any], paths: InstancePaths) -> EchoCore:
    return EchoCore()


factory.display_name = "Echo (no AI)"  # type: ignore[attr-defined]
