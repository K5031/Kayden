from interfaces import ContextInterface
from klogger import get_logger

DEFAULT_MAX_CONTEXT_TOKENS = 60000
MAX_TOKENS_PER_EXCHANGE = 3000
TRIM_TARGET_RATIO = 0.7
CHARS_PER_TOKEN_ESTIMATE = 3.5


class Module(ContextInterface):
    usage_mode = "reported"

    def __init__(self, max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS):
        self.conversation = []
        self.max_context_tokens = max_context_tokens
        self.log = get_logger("Context")
        self.last_known_tokens = 0

    def report_usage(self, prompt_tokens: int) -> None:
        self.last_known_tokens = prompt_tokens

    def _would_overflow(self) -> bool:
        return self.last_known_tokens + MAX_TOKENS_PER_EXCHANGE > self.max_context_tokens

    def add(self, role: str, content: str) -> None:
        self.conversation.append({"role": role, "content": content})

        dropped = 0
        if self._would_overflow():
            target = int(self.max_context_tokens * TRIM_TARGET_RATIO)
            while self.last_known_tokens > target and len(self.conversation) > 1:
                removed = self.conversation.pop(0)
                self.last_known_tokens -= int(len(removed["content"]) / CHARS_PER_TOKEN_ESTIMATE)
                dropped += 1

        if dropped:
            self.log.info("trimmed %d oldest message(s) to stay under %d token budget "
                          "(last known: %d tokens, %d messages)",
                          dropped, self.max_context_tokens, self.last_known_tokens,
                          len(self.conversation))

    def get(self) -> list[dict]:
        return self.conversation

    def clear(self) -> None:
        self.log.info("conversation cleared (%d messages, last known %d tokens)",
                      len(self.conversation), self.last_known_tokens)
        self.conversation = []
        self.last_known_tokens = 0
