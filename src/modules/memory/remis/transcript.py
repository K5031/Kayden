from interfaces import CompactionPlan, MemoryView


class EphemeralTranscript:
    """In-process transcript support for the REMIS memory module."""

    def _initialize_transcript(self):
        self._conversation = []
        self._summary = ""
        self._through_index = 0

    def append(self, messages: list[dict]) -> None:
        self._conversation.extend(dict(message) for message in messages)

    def view(self) -> MemoryView:
        return MemoryView(
            summary=self._summary,
            messages=[
                dict(message)
                for message in self._conversation[self._through_index:]
            ],
        )

    def recent(self, limit: int = 50) -> list[dict]:
        return [dict(message) for message in self._conversation[-limit:]]

    def plan_compaction(self, keep_recent: int) -> CompactionPlan | None:
        keep_recent = max(2, int(keep_recent))
        active = self._conversation[self._through_index:]
        older = active[:-keep_recent]
        if not older:
            return None
        return CompactionPlan(
            previous_summary=self._summary,
            messages=[dict(message) for message in older],
            base_checkpoint=self._through_index,
            through_checkpoint=self._through_index + len(older),
        )

    def commit_compaction(self, plan: CompactionPlan, summary: str) -> bool:
        if self._through_index != plan.base_checkpoint or not summary.strip():
            return False
        self._through_index = plan.through_checkpoint
        self._summary = summary.strip()
        return True

    def reset_context(self) -> None:
        self._summary = ""
        self._through_index = len(self._conversation)
