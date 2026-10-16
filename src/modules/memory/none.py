from interfaces import MemoryInterface


class Module(MemoryInterface):
    """No-op memory module for installations that do not need persistence."""

    def store(self, messages: list[dict]) -> None:
        return None

    def retrieve(self, query: str) -> str:
        return ""
