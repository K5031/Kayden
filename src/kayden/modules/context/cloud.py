from kayden.interfaces import ContextPolicyInterface, MemoryView

DEFAULT_MAX_CONTEXT_TOKENS = 60000
CHARS_PER_TOKEN_ESTIMATE = 3.5
DEFAULT_RECENT_VERBATIM_TOKENS = 35000
DEFAULT_SUMMARY_TARGET_TOKENS = 4000
DEFAULT_DURABLE_MEMORY_TOKENS = 2000
DEFAULT_TARGET_FREE_TOKENS = 19000
DEFAULT_MINIMUM_FREE_TOKENS = 10000


class Module(ContextPolicyInterface):
    usage_mode = "reported"

    def __init__(
        self,
        max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
        recent_verbatim_tokens: int = DEFAULT_RECENT_VERBATIM_TOKENS,
        summary_target_tokens: int = DEFAULT_SUMMARY_TARGET_TOKENS,
        durable_memory_tokens: int = DEFAULT_DURABLE_MEMORY_TOKENS,
        target_free_tokens: int = DEFAULT_TARGET_FREE_TOKENS,
        minimum_free_tokens: int = DEFAULT_MINIMUM_FREE_TOKENS,
    ):
        self.max_context_tokens = max(1, int(max_context_tokens))
        self.summary_target_tokens = max(1, int(summary_target_tokens))
        self.durable_memory_tokens = max(0, int(durable_memory_tokens))
        self.target_free_tokens = max(1, int(target_free_tokens))
        self.minimum_free_tokens = max(1, int(minimum_free_tokens))
        configured_recent = max(1, int(recent_verbatim_tokens))
        component_budget = max(
            1,
            self.max_context_tokens
            - self.target_free_tokens
            - self.summary_target_tokens
            - self.durable_memory_tokens,
        )
        self.recent_verbatim_tokens = min(configured_recent, component_budget)
        self.last_known_tokens = 0

    def report_usage(self, prompt_tokens: int) -> None:
        self.last_known_tokens = prompt_tokens

    @staticmethod
    def _estimate(messages: list[dict]) -> int:
        return max(0, int(
            sum(len(message.get("content", "")) for message in messages)
            / CHARS_PER_TOKEN_ESTIMATE
        ))

    @staticmethod
    def _turns(messages: list[dict]) -> list[list[dict]]:
        """Group transcript messages without splitting user/assistant turns."""
        turns = []
        current = []
        for message in messages:
            if message.get("role") == "user" and current:
                turns.append(current)
                current = []
            current.append(message)
        if current:
            turns.append(current)
        return turns

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
        return known_tokens + self.minimum_free_tokens > self.max_context_tokens

    def retained_message_count(self, view: MemoryView) -> int:
        retained = 0
        used = 0
        for turn in reversed(self._turns(view.messages)):
            turn_tokens = self._estimate(turn)
            if retained and used + turn_tokens > self.recent_verbatim_tokens:
                break
            retained += len(turn)
            used += turn_tokens
        return retained

    def constrain_summary(self, summary: str) -> str:
        summary = summary.strip()
        max_characters = int(self.summary_target_tokens * CHARS_PER_TOKEN_ESTIMATE)
        if len(summary) <= max_characters:
            return summary
        bounded = summary[:max_characters].rstrip()
        if "\n" in bounded:
            bounded = bounded.rsplit("\n", 1)[0].rstrip()
        elif " " in bounded:
            bounded = bounded.rsplit(" ", 1)[0].rstrip()
        return bounded

    def compacted(self, view: MemoryView) -> None:
        self.last_known_tokens = self._estimate(self.prepare(view))

    def reset(self) -> None:
        self.last_known_tokens = 0
