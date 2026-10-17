import json
import re

from kayden.interfaces import ToolInterface


KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
MAX_HISTORY_RESULT_CHARACTERS = 12000


class MemoryActions(ToolInterface):
    """Model-facing actions provided by the continuity memory backend."""

    def __init__(self, memory):
        self.memory = memory

    def schemas(self) -> list[dict]:
        key = {
            "type": "string",
            "description": (
                "Stable lowercase key such as profile.name, preference.tone, "
                "or plan.current_project. Reuse a key to update it."
            ),
        }
        return [
            {
                "type": "function",
                "function": {
                    "name": "remember",
                    "description": (
                        "Save or update one durable fact the user explicitly wants "
                        "remembered, or a stable preference they clearly stated."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "key": key,
                            "value": {
                                "type": "string",
                                "description": "A concise self-contained fact.",
                            },
                        },
                        "required": ["key", "value"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "forget_memory",
                    "description": "Delete one named durable memory at the user's request.",
                    "parameters": {
                        "type": "object",
                        "properties": {"key": key},
                        "required": ["key"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_memories",
                    "description": "List the durable facts currently remembered about the user.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_history",
                    "description": (
                        "Search earlier conversation when the user refers to something "
                        "absent from the current context or durable memories."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Important words likely used earlier.",
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 20,
                                "description": "Maximum matches to return (default 8).",
                            },
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    @staticmethod
    def _validate_arguments(arguments: dict, allowed: set[str]) -> None:
        if not set(arguments).issubset(allowed):
            raise ValueError("unexpected arguments")

    @staticmethod
    def _key(arguments: dict) -> str:
        key = arguments.get("key")
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
            raise ValueError(
                "key must start with a lowercase letter and contain only "
                "lowercase letters, numbers, dots, dashes, or underscores"
            )
        return key

    def execute(self, name: str, arguments: dict) -> str:
        if name == "remember":
            self._validate_arguments(arguments, {"key", "value"})
            key = self._key(arguments)
            value = arguments.get("value")
            if not isinstance(value, str) or not value.strip():
                raise ValueError("value must be a non-empty string")
            if len(value) > 1000:
                raise ValueError("value is too long")
            self.memory.remember(key, value.strip())
            return f"Remembered {key}."

        if name == "forget_memory":
            self._validate_arguments(arguments, {"key"})
            key = self._key(arguments)
            if self.memory.forget(key):
                return f"Forgot {key}."
            return f"No memory named {key}."

        if name == "list_memories":
            self._validate_arguments(arguments, set())
            memories = self.memory.remembered()
            if not memories:
                return "[no durable memories]"
            return "\n".join(
                f"{memory['key']}: {memory['value']}" for memory in memories
            )

        if name == "search_history":
            self._validate_arguments(arguments, {"query", "limit"})
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("query must be a non-empty string")
            limit = arguments.get("limit", 8)
            if (
                not isinstance(limit, int)
                or isinstance(limit, bool)
                or not 1 <= limit <= 20
            ):
                raise ValueError("limit must be an integer from 1 to 20")
            matches = self.memory.search(query, limit)
            if not matches:
                return "[no matching conversation]"
            bounded = []
            remaining = MAX_HISTORY_RESULT_CHARACTERS
            for match in matches:
                if remaining <= 0:
                    break
                content = match["content"]
                if len(content) > remaining:
                    content = content[:remaining] + "\n[message truncated]"
                bounded.append({
                    "role": match["role"],
                    "content": content,
                    "created_at": match["created_at"],
                })
                remaining -= len(content)
            return json.dumps(bounded, ensure_ascii=False)

        raise ValueError(f"Unknown tool: {name}")
