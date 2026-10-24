from llama_cpp import Llama

from kayden.config import resolve_model_path
from kayden.interfaces import ContextPolicyInterface, MemoryView

DEFAULT_MODEL = "gemma-4-26B-A4B-it-UD-Q4_K_M.gguf"
DEFAULT_MAX_CONTEXT_TOKENS = 15600
DEFAULT_SUMMARY_TARGET_TOKENS = 2000
DEFAULT_DURABLE_MEMORY_TOKENS = 1000
DEFAULT_HISTORICAL_SUMMARY_TOKENS = 500
COMPACTION_TRIGGER_RATIO = 0.8


class Module(ContextPolicyInterface):
    usage_mode = "self"

    def __init__(
        self,
        model_path=DEFAULT_MODEL,
        max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
        keep_recent_messages=12,
        summary_target_tokens: int = DEFAULT_SUMMARY_TARGET_TOKENS,
        durable_memory_tokens: int = DEFAULT_DURABLE_MEMORY_TOKENS,
        historical_summary_tokens: int = DEFAULT_HISTORICAL_SUMMARY_TOKENS,
    ):
        self.max_context_tokens = max_context_tokens
        self.keep_recent_messages = max(2, int(keep_recent_messages))
        self.summary_target_tokens = max(1, int(summary_target_tokens))
        self.durable_memory_tokens = max(1, int(durable_memory_tokens))
        self.historical_summary_tokens = max(
            1, int(historical_summary_tokens)
        )

        resolved_path = resolve_model_path(model_path)

        self.tokenizer = Llama(model_path=resolved_path, vocab_only=True, verbose=False)

    def _count_tokens(self, text: str) -> int:
        return len(self.tokenizer.tokenize(text.encode("utf-8")))

    def estimate_tokens(self, text: str) -> int:
        return self._count_tokens(text)

    def _total_tokens(self, messages: list[dict]) -> int:
        return sum(self._count_tokens(m["content"]) for m in messages)

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
        return (
            self._total_tokens(self.prepare(view))
            > self.max_context_tokens * COMPACTION_TRIGGER_RATIO
        )

    def stop(self) -> None:
        close = getattr(self.tokenizer, "close", None)
        if close:
            close()
