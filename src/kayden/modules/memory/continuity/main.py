"""Continuous-memory module entrypoint."""

from kayden.interfaces import CompactionPlan, MemoryInterface, MemoryView, ToolInterface
from kayden.modules.memory.continuity.actions import MemoryActions
from kayden.modules.memory.continuity.store import ContinuityStore


class Module(MemoryInterface):
    """Own Kayden's persistent transcript, facts, summaries, and recall."""

    def __init__(self, database_path=None, max_characters=6000):
        self.backend = ContinuityStore(database_path)
        self.max_characters = max(256, int(max_characters))
        self._actions = MemoryActions(self)

    def append(self, messages: list[dict]) -> None:
        for message in messages:
            self.backend.add_message(message["role"], message["content"])

    def recall(self, query: str) -> str:
        lines = []
        used = 0
        for memory in self.backend.memories():
            line = f"- {memory['key']}: {memory['value']}"
            if used + len(line) > self.max_characters:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)

    def view(self) -> MemoryView:
        summary, through_id = self.backend.context_state()
        return MemoryView(
            summary=summary,
            messages=[
                {"role": message["role"], "content": message["content"]}
                for message in self.backend.messages_after(through_id)
            ],
        )

    def recent(self, limit: int = 50) -> list[dict]:
        return [
            {"role": message["role"], "content": message["content"]}
            for message in self.backend.recent_messages(limit)
        ]

    def plan_compaction(self, keep_recent: int) -> CompactionPlan | None:
        summary, through_id = self.backend.context_state()
        active = self.backend.messages_after(through_id)
        older = active[:-max(1, int(keep_recent))]
        if not older:
            return None
        return CompactionPlan(
            previous_summary=summary,
            messages=[
                {"role": message["role"], "content": message["content"]}
                for message in older
            ],
            base_checkpoint=through_id,
            through_checkpoint=older[-1]["id"],
        )

    def commit_compaction(self, plan: CompactionPlan, summary: str) -> bool:
        if not summary.strip():
            return False
        return self.backend.commit_context_state(
            plan.base_checkpoint,
            summary.strip(),
            plan.through_checkpoint,
        )

    def reset_context(self) -> None:
        self.backend.set_context_state("", self.backend.latest_message_id())

    def actions(self) -> ToolInterface:
        return self._actions

    def remember(self, key: str, value: str) -> None:
        self.backend.remember(key, value)

    def forget(self, key: str) -> bool:
        return self.backend.forget(key)

    def remembered(self) -> list[dict]:
        return self.backend.memories()

    def search(self, query: str, limit: int = 8) -> list[dict]:
        return self.backend.search_messages(query, limit)


__all__ = ["ContinuityStore", "Module"]
