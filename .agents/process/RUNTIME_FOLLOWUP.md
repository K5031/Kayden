# Runtime follow-up: contract Draft 2.3

Your `.agents/contract-issues.md` was read. Most of your assumptions are now the spec. `SPEC.md` and `src/kayden/api.py` have been updated (replace them with the new copies; do not edit them yourself). Adapt the runtime and tests to the changes below, then update `.agents/contract-issues.md`: mark each entry resolved, accepted, or still open.

## Changes you must make
1. **Barrier ordering (your 2.1).** A non-read-only call starts only after every earlier call in the turn has finished or been backgrounded, and later calls wait for it. Consecutive read-only calls still run concurrently. Test `[write, read]`, `[read, write]` and `[read, read, write, read]`.
2. **Duration exhaustion (4.5).** When the duration budget is exhausted, cancel running operations as Stop does (grace, then abandon), in addition to cancelling inference.
3. **Unbilled failures (4.9).** A model request that fails before its first stream event is not charged.
4. **Activation notice (3.8).** `activate()` with no new message: if the transcript is empty or its newest message is not a USER message, first append a short runtime notice as a USER message. The same applies to the first cycle of an empty Continuous conversation.
5. **Continuous `start()` (3.4).** A `conversation_id` that differs from the instance's single continuous conversation raises `ValueError`.
6. **`op_status` (2.8).** Clamp `wait_s` to 60 s (the schema now says so).
7. **`AgentState.NOT_STARTED` (6.1).** `snapshot().state` is `NOT_STARTED` before `start()`.
8. **`MessageCommitted.stop` (6.5).** Set it to the stop reason on assistant messages.
9. **Use the shared helpers.** `InstancePaths` comes from `api.py`; `PermissionPolicy.inherited_ceiling()` now returns DENY for an invalid value; `PermissionPolicy.clamp` does the clamping.
10. **Python.** Target 3.12+ and run on whatever `uv` provides. `AGENTS.md` saying 3.14 is a preference, not a requirement.

## Already matches the new spec (keep as is)
ToolFinished buffering for foreground calls; one USER message per queued message; Stop in IDLE with outstanding operations performs a full Stop; wake set computed at the turn boundary; idempotent append backlog and FAILED(INTERNAL) with `activate()` retry; per-mode usage file and lock; invalid ceiling fail-closed; any exhausted dimension stops all chargeable execution; ABANDONED for unconfirmed timeouts.

## Report
Add a short section to `.agents/decisions.md` listing what changed, and keep the full test suite and `ruff` green. Commit.