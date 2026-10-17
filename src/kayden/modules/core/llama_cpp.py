from typing import Callable, Iterator

from llama_cpp import Llama

from kayden.config import resolve_model_path
from kayden.events import CoreEvent, CoreTextDelta
from kayden.interfaces import CoreInterface
from kayden.klogger import get_logger

DEFAULT_MODEL = "gemma-4-26B-A4B-it-UD-Q4_K_M.gguf"
DEFAULT_N_GPU_LAYERS = 5
DEFAULT_N_CTX = 16384


class Module(CoreInterface):
    runtime_kind = "local"

    def __init__(
        self,
        model_path=DEFAULT_MODEL,
        n_gpu_layers=DEFAULT_N_GPU_LAYERS,
        n_ctx=DEFAULT_N_CTX,
    ):
        self.log = get_logger("Core")

        resolved_path = resolve_model_path(model_path)
        self.model_path = resolved_path

        self.log.info("Loading model: %s", resolved_path)
        self.llm = Llama(
            model_path=resolved_path,
            n_ctx=n_ctx,
            offload_kqv=True,
            n_gpu_layers=n_gpu_layers,
            main_gpu=0,
            n_threads=4,
            n_threads_batch=4,
            n_batch=512,
            n_ubatch=512,
            use_mmap=True,
            cache=True,
            verbose=False,
        )
        self._interrupted = False

    def interrupt(self):
        self._interrupted = True

    def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        should_interrupt: Callable[[], bool] | None = None,
    ) -> Iterator[CoreEvent]:
        if tools:
            raise NotImplementedError("The llama_cpp core does not support tools yet")
        self._interrupted = False

        self.log.debug("messages: %s", messages)
        for chunk in self.llm.create_chat_completion(
            messages=messages,
            stream=True,
            temperature=0.45,
            top_p=0.85,
            repeat_penalty=1.1,
            max_tokens=512,
        ):
            if self._interrupted or (should_interrupt and should_interrupt()):
                try:
                    self.llm.reset()
                except Exception as error:
                    self.log.warning("reset after interrupt failed: %s", error)
                break
            token = chunk["choices"][0]["delta"].get("content", "")
            if token:
                yield CoreTextDelta(token)

    def stop(self) -> None:
        close = getattr(self.llm, "close", None)
        if close:
            close()
