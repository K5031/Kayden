from interfaces import ContextPolicyInterface, MemoryView

DEFAULT_MAX_CONTEXT_TOKENS = 60000
MAX_TOKENS_PER_EXCHANGE = 3000
CHARS_PER_TOKEN_ESTIMATE = 3.5


class Module(ContextPolicyInterface):
    usage_mode = "reported"

    def __init__(
        self,
        max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
        keep_recent_messages=12,
    ):
        self.max_context_tokens = max_context_tokens
        self.keep_recent_messages = max(2, int(keep_recent_messages))
        self.last_known_tokens = 0

    def report_usage(self, prompt_tokens: int) -> None:
        self.last_known_tokens = prompt_tokens

    @staticmethod
    def _estimate(messages: list[dict]) -> int:
        return int(
            sum(len(message.get("content", "")) for message in messages)
            / CHARS_PER_TOKEN_ESTIMATE
        )

    def prepare(self, view: MemoryView) -> list[dict]:
        messages = []
        if view.summary:
            messages.append({
                "role": "system",
                "content": "Summary of earlier conversation:\n" + view.summary,
            })
        messages.extend(dict(message) for message in view.messages)
        return messages

    def needs_compaction(self, view: MemoryView) -> bool:
        known_tokens = max(self.last_known_tokens, self._estimate(self.prepare(view)))
        return known_tokens + MAX_TOKENS_PER_EXCHANGE > self.max_context_tokens

    def compacted(self, view: MemoryView) -> None:
        self.last_known_tokens = self._estimate(self.prepare(view))

    def reset(self) -> None:
        self.last_known_tokens = 0
