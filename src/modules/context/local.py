from llama_cpp import Llama

from interfaces import ContextInterface
from klogger import get_logger
from utils import resolve_model_path

DEFAULT_MODEL = "gemma-4-26B-A4B-it-UD-Q4_K_M.gguf"
DEFAULT_MAX_CONTEXT_TOKENS = 15600
TRIM_TARGET_RATIO = 0.7


class Module(ContextInterface):
    usage_mode = "self"

    def __init__(self, model_path=DEFAULT_MODEL, max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS):
        self.conversation = []
        self.max_context_tokens = max_context_tokens
        self.log = get_logger("Context")

        resolved_path = resolve_model_path(model_path)

        self.tokenizer = Llama(model_path=resolved_path, vocab_only=True, verbose=False)

    def _count_tokens(self, text: str) -> int:
        return len(self.tokenizer.tokenize(text.encode("utf-8")))

    def _total_tokens(self) -> int:
        return sum(self._count_tokens(m["content"]) for m in self.conversation)

    def add(self, role: str, content: str) -> None:
        self.conversation.append({"role": role, "content": content})

        dropped = 0
        if self._total_tokens() > self.max_context_tokens:
            target = int(self.max_context_tokens * TRIM_TARGET_RATIO)
            while self._total_tokens() > target and len(self.conversation) > 1:
                self.conversation.pop(0)
                dropped += 1

        if dropped:
            self.log.info("trimmed %d oldest message(s) to stay under %d token budget "
                          "(now %d tokens, %d messages)",
                          dropped, self.max_context_tokens, self._total_tokens(),
                          len(self.conversation))

    def get(self) -> list[dict]:
        return self.conversation

    def clear(self) -> None:
        self.log.info("conversation cleared (%d messages, %d tokens)",
                      len(self.conversation), self._total_tokens())
        self.conversation = []

    def stop(self) -> None:
        close = getattr(self.tokenizer, "close", None)
        if close:
            close()
