import json
import re

from kayden.interfaces import ToolInterface


KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
NON_ATOMIC_VALUE_PATTERN = re.compile(
    r"^(?:the\s+)?user\s+(?:"
    r"is|has|had|likes?|loves?|prefers?|wants?|needs?|works?|"
    r"stud(?:y|ies|ied)|attends?|goes?|lives?|uses?|owns?|"
    r"knows?|speaks?|plays?|builds?"
    r")\b",
    re.IGNORECASE,
)
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
                "or plan.current_project. The key owns the subject of exactly "
                "one fact; reuse that same key to update it."
            ),
        }
        return [
            {
                "type": "function",
                "function": {
                    "name": "remember",
                    "description": (
                        "Save or update exactly one atomic durable fact the user "
                        "explicitly wants remembered, or a stable preference they "
                        "clearly stated. Split independent facts across separate keys."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "key": key,
                            "value": {
                                "type": "string",
                                "description": (
                                    "Only the value belonging to the key, not a sentence "
                                    "and not facts owned by other keys. Examples: use "
                                    "profile.university='UCL' and "
                                    "profile.degree_subject='Computer Science'; never "
                                    "profile.degree_subject='User studies Computer Science "
                                    "at UCL'."
                                ),
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

    def describe_activity(self, name, arguments, state, result=None):
        key = " ".join(str(arguments.get("key", "")).split())[:72]
        query = " ".join(str(arguments.get("query", "")).split())[:72]
        descriptions = {
            "remember": (
                f"Saving memory “{key}”" if key else "Saving a memory",
                f"Saved memory “{key}”" if key else "Saved a memory",
                "Couldn’t save the memory",
            ),
            "forget_memory": (
                f"Forgetting memory “{key}”" if key else "Forgetting a memory",
                f"Forgot memory “{key}”" if key else "Forgot a memory",
                "Couldn’t forget the memory",
            ),
            "list_memories": (
                "Checking saved memories",
                "Checked saved memories",
                "Couldn’t check saved memories",
            ),
            "search_history": (
                f"Searching conversation history for “{query}”",
                f"Searched conversation history for “{query}”",
                "Conversation search failed",
            ),
        }
        running, finished, failed = descriptions[name]
        return {"running": running, "finished": finished, "failed": failed}[state]

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

    @staticmethod
    def _value(arguments: dict) -> str:
        value = arguments.get("value")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("value must be a non-empty string")
        value = value.strip()
        if len(value) > 1000:
            raise ValueError("value is too long")
        if "\n" in value or "\r" in value:
            raise ValueError("value must describe one atomic fact on one line")
        if NON_ATOMIC_VALUE_PATTERN.match(value):
            raise ValueError(
                "value must contain only the atomic value owned by the key, "
                "not a sentence about the user or facts from other keys"
            )
        return value

    def execute(self, name: str, arguments: dict) -> str:
        if name == "remember":
            self._validate_arguments(arguments, {"key", "value"})
            key = self._key(arguments)
            value = self._value(arguments)
            self.memory.remember(key, value)
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
