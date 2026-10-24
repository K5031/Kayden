from kayden.interfaces import ToolInterface
from kayden.permissions import OperationDefinition


class MemoryActions(ToolInterface):
    """Optional immediate actions for continuity's consolidated text memory."""

    def __init__(self, memory):
        self.memory = memory

    def schemas(self) -> list[dict]:
        memory_content = {
            "type": "string",
            "description": (
                "One concise, self-contained fact or preference in natural language. "
                "Do not invent a key or include unrelated information."
            ),
        }
        return [
            {
                "type": "function",
                "function": {
                    "name": "search_history",
                    "description": (
                        "Search permanent summaries of older conversation when "
                        "the user refers to a past discussion, decision, or experience."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Concise keywords describing the past topic.",
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 20,
                                "default": 3,
                            },
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "remember",
                    "description": (
                        "Immediately add one explicit memory to long-term text memory. "
                        "Ordinary important information is also reviewed automatically "
                        "during context consolidation."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"content": memory_content},
                        "required": ["content"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "forget_memory",
                    "description": (
                        "Remove one exact line from long-term text memory at the "
                        "user's request. Use list_memories first if its wording is unknown."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"content": memory_content},
                        "required": ["content"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_memories",
                    "description": "Read the current consolidated long-term memory.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            },
        ]

    @staticmethod
    def _content(arguments: dict) -> str:
        if set(arguments) != {"content"}:
            raise ValueError("arguments must contain only content")
        content = arguments.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be non-empty text")
        content = " ".join(content.split())
        if len(content) > 1000:
            raise ValueError("content is too long")
        return content

    def describe_activity(self, name, arguments, state, result=None):
        content = " ".join(str(arguments.get("content", "")).split())[:72]
        descriptions = {
            "search_history": (
                "Searching conversation history",
                "Searched conversation history",
                "Couldn’t search conversation history",
            ),
            "remember": (
                f"Saving “{content}”" if content else "Saving a memory",
                "Updated long-term memory",
                "Couldn’t update long-term memory",
            ),
            "forget_memory": (
                f"Forgetting “{content}”" if content else "Forgetting a memory",
                "Updated long-term memory",
                "Couldn’t update long-term memory",
            ),
            "list_memories": (
                "Checking long-term memory",
                "Checked long-term memory",
                "Couldn’t check long-term memory",
            ),
        }
        running, finished, failed = descriptions[name]
        return {"running": running, "finished": finished, "failed": failed}[state]

    def operation(self, name, arguments):
        if name == "forget_memory":
            content = " ".join(str(arguments.get("content", "")).split())[:72]
            label = f"Forget “{content}”" if content else "Forget a memory"
            return OperationDefinition(
                id="tool.forget_memory",
                label=label,
                effects=frozenset({"memory.delete"}),
            )
        return super().operation(name, arguments)

    def execute(self, name: str, arguments: dict) -> str:
        if name == "search_history":
            if not isinstance(arguments, dict) or not set(arguments) <= {
                "query", "limit"
            }:
                raise ValueError("arguments may contain only query and limit")
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("query must be non-empty text")
            limit = arguments.get("limit", 3)
            if not isinstance(limit, int) or isinstance(limit, bool):
                raise ValueError("limit must be an integer")
            return self.memory.search_history(query, limit)

        if name == "remember":
            content = self._content(arguments)
            added = self.memory.remember_text(content)
            return "Remembered." if added else "Already remembered."

        if name == "forget_memory":
            content = self._content(arguments)
            if self.memory.forget_text(content):
                return "Forgot it."
            return "No exact memory matched that text."

        if name == "list_memories":
            if arguments:
                raise ValueError("list_memories does not accept arguments")
            return self.memory.memory_document() or "[no durable memories]"

        raise ValueError(f"Unknown tool: {name}")
