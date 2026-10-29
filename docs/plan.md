# Clean-room runtime plan

Behaviors extracted from `SPEC.md` and `src/kayden/api.py`, each mapped to the test that
covers it. Test files live in `tests/`; fakes in `tests/fakes/`.

## Runtime surface (implementation choices, see `decisions.md`)

- `kayden.runtime.Runtime(core, memory, tools, frontend: FrontendInfo, config: RuntimeConfig)`
  implements `RuntimeAPI`; `await runtime.aclose()` shuts down.
- `kayden.runtime.run_instance(InstanceConfig)` loads the four modules from
  `"package.module:factory"` import paths, runs `frontend.run(runtime)`, then shuts down.
- `kayden.runtime.InstanceLockedError` when the (data dir, mode) lock is held.

## Acceptance scenarios (SPEC 12) — `tests/test_scenarios.py`

| Id | Behavior | Test |
|----|----------|------|
| A | Modules are loaded from import-path factories; swapping a factory needs no runtime change | `test_a_replaceable_modules` |
| B | Session: send, tool call, shutdown, reopen; history preserved | `test_b_session_conversation` |
| C | Continuous agent reasons, uses a tool, idles, is woken by batch completion (agent-controlled), stays idle (user-controlled) | `test_c_continuous_autonomy` |
| D | Slow tool backgrounded with a placeholder; model inspects progress with `op_status`; outcome arrives as `OperationResult` | `test_d_long_running_tool` |
| E | Concurrent batch + `idle()`: agent-controlled wakes once after all terminate; user-controlled does not restart | `test_e_batch_wakeup_agent_controlled`, `test_e_batch_user_controlled_retains` |
| F | Stop during inference and during tools: cancellation, no wake-up, state preserved | `test_f_stop_during_inference`, `test_f_stop_during_tools` |
| G | Restart restores history, summary, archive; continuous comes back STOPPED | `test_g_restart_persistence` |
| H | After repeated compactions `recall` retrieves original messages | `test_h_historical_recall` |
| I | `peek_other_mode` reads the other mode without changing it | `test_i_cross_mode_memory` |
| J | Three instances: separate memory, config, budgets; coordinate via an ordinary tool | `test_j_multi_instance_workforce` |
| K | Transient failures retried with backoff; persistent failure → FAILED(MODEL), no model switch | `test_k_transient_then_success`, `test_k_persistent_failure` |
| L | Exhausted hard budget prevents further chargeable execution | `test_l_budget_enforcement` |

## Runtime guarantees (api.py docstring) — `tests/test_guarantees.py`

| Guarantee | Test |
|-----------|------|
| Assistant message appended before any of its tool calls starts | `test_assistant_appended_before_tool_start` |
| ToolResult appended before its ToolFinished | `test_tool_result_appended_before_tool_finished` |
| User message appended before its MessageCommitted | `test_user_message_appended_before_committed` |
| Append failure → Failure(INTERNAL), nothing further starts until an append succeeds | `test_append_failure_stops_further_work` |
| append/maintain serialized; maintain after results, before next assemble, never during inference | `test_maintain_ordering_and_serialization` |
| Stop cancels maintain | `test_stop_cancels_maintain` |
| Budget re-checked after maintain | `test_budget_rechecked_after_maintain` |
| Resume repair: ToolResult(INTERRUPTED), OperationResult(INTERRUPTED) | `test_resume_repair` |
| Validation: unknown name / schema mismatch / unparsed args → ERROR, tool not run | `test_tool_call_validation` |
| Wrapping: tool exception → ERROR result; Core unexpected exception → permanent | `test_tool_exception_wrapped`, `test_core_unexpected_exception_is_permanent` |
| Wrapping: Memory / Tools.list_tools exceptions → Failure, no crash | `test_module_exceptions_become_failures` |
| One inference cycle at a time; triggers only set flags | `test_single_cycle_at_a_time` |
| Turn boundary order: tool results, op results (one USER msg), queued messages | `test_turn_boundary_order` |
| Budgets reserved atomically before tool start | `test_tool_call_budget_reserved_atomically` |
| Message conventions: one TOOL message per turn right after the assistant, one result per call | `test_one_tool_message_per_turn` |

## Command × state table (RuntimeAPI) — `tests/test_commands.py`

One parametrized test per cell: 7 states (not started, IDLE, THINKING, RUNNING_TOOLS,
AWAITING_PERMISSION, STOPPING, STOPPED, FAILED; THINKING/TOOLS split into two rows) × 6
commands (`test_command_state_cell`). Footnotes: `test_start_refused_with_outstanding_ops`,
`test_mode_cannot_change`, `test_activate_idle_agent_controlled_raises`,
`test_failed_budget_blocks_send_and_activate`, `test_resolve_permission_unknown_and_twice`,
`test_cancel_operation_requires_running_call`.

## Budgets (SPEC 9) — `tests/test_budget.py`

| Behavior | Test |
|----------|------|
| Token budget counts input (cached included) + output | `test_tokens_count_input_and_output` |
| Cost from provider report; cost from model prices | `test_cost_reported`, `test_cost_from_prices` |
| Cost budget without prices refuses start / select_model | `test_cost_budget_requires_prices` |
| Duration counts tool and inference time incl. background; excludes permission waits, backoff | `test_duration_counts_running_time`, `test_duration_excludes_permission_wait_and_backoff` |
| Built-ins are not tool calls | `test_builtins_not_counted` |
| Checked before each inference and tool start; refused calls recorded | `test_tool_refused_when_exhausted` |
| Exhausted mid-stream → inference cancelled, FAILED(BUDGET) | `test_mid_stream_exhaustion` |
| Failed/cancelled request charged max(last report, estimate) | `test_failed_request_charged` |
| Usage persists across restarts; only explicit reset | `test_usage_persists` |
| Completion raises BudgetExhausted | `test_completion_budget_exhausted` |

## Other runtime behavior — `tests/test_runtime.py`, `tests/test_tools_exec.py`

| Behavior | Test |
|----------|------|
| Permission policies DENY / ASK / ALLOW_READ_ONLY, grants, non-interactive frontend, first-wins, Stop denies prompts, built-ins ungated | `test_permission_*` |
| Ceiling: clamp to inherited, export effective policy | `test_permission_ceiling` |
| Instance lock per (dir, mode), error names holder | `test_instance_lock` |
| Event stream: gapless seq, bounded queue drops oldest, ends after ShuttingDown; snapshot recovery | `test_event_*`, `test_snapshot_*` |
| JSONL event log of decisions | `test_jsonl_event_log` |
| Backoff delays and retry_after | `test_retry_backoff_delays` |
| ContextOverflowError → re-assemble 10% smaller, retry once, then fail | `test_context_overflow_*` |
| MAX_TOKENS turn committed; unparsed call → ERROR, cycle continues | `test_max_tokens_truncated_call` |
| Model selection validated, ModelSelected, applies next inference | `test_select_model` |
| Initial wait → backgrounded placeholder + ToolBackgrounded | `test_backgrounding` |
| Stall timeout and hard timeout → TIMED_OUT; default timeout | `test_stall_timeout`, `test_hard_timeout`, `test_default_timeout` |
| Abandon after grace; late completion logged, never appended | `test_abandon_and_late_completion` |
| progress() from threads | `test_thread_tool_progress` |
| cancel_operation / op_cancel / op_status | `test_cancel_operation`, `test_op_cancel`, `test_op_status_wait` |
| Read-only calls concurrent, others in emission order | `test_call_ordering` |
| Reserved names cannot be redeclared | `test_reserved_names_dropped` |
| Stop commits partial text only; queued-while-stopping starts a cycle once stopped; StopStalled | `test_stop_*` |
| Idle wait_for subset; unseen results wake immediately; idle absent in Session | `test_idle_*` |
| Session may switch conversation only without outstanding ops | `test_session_switch_conversation` |
| Background outcome appended immediately when no cycle runs | `test_background_result_appended_when_idle` |
| Graceful shutdown cancels running calls (CANCELLED, not INTERRUPTED) | `test_shutdown_cancels_calls` |
