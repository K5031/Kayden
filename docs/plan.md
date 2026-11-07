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
| ContextOverflowError → re-assemble with 90% of the context budget, retry once, then fail | `test_context_overflow_*` |
| MAX_TOKENS turn committed; unparsed call → ERROR, cycle continues | `test_max_tokens_truncated_call` |
| Model selection validated, ModelSelected, applies next inference | `test_select_model` |
| Initial wait → backgrounded placeholder + ToolBackgrounded | `test_backgrounding` |
| Stall timeout and hard timeout → TIMED_OUT; default timeout | `test_stall_timeout`, `test_hard_timeout`, `test_default_timeout` |
| Abandon after grace; late completion logged, never appended | `test_abandon_and_late_completion` |
| progress() from threads | `test_thread_tool_progress` |
| cancel_operation / op_cancel / op_status | `test_cancel_operation`, `test_op_cancel`, `test_op_status_wait_clamped` |
| Read-only calls concurrent; a non-read-only call is a barrier; a backgrounded call releases it | `test_barrier_*`, `test_backgrounded_call_releases_barrier` |
| Reserved names cannot be redeclared | `test_reserved_names_dropped` |
| Stop commits partial text only; queued-while-stopping starts a cycle once stopped; StopStalled | `test_stop_*` |
| Idle wait_for subset; unseen results wake immediately; idle absent in Session | `test_idle_*` |
| Session may switch conversation only without outstanding ops | `test_session_switch_conversation` |
| Background outcome appended immediately when no cycle runs | `test_background_result_appended_when_idle` |
| Graceful shutdown ends running calls as Stop does: CANCELLED, or ABANDONED when cancel is ignored; never INTERRUPTED | `test_shutdown_cancels_calls`, `test_shutdown_abandons_calls_that_ignore_cancel` |
| Stop before a requested cycle begins: message kept, no inference | `test_stop_before_cycle_starts` |
| Messages queued before Stop are kept but start nothing | `test_stop_keeps_message_queued_before_stop` |
| Maintenance model validated; `Completion(light=True)` uses it; Maintenance events | `test_maintenance_model` |
| Completion requests are marked `maintenance`; max_tokens capped at the model's output limit | `test_only_completion_requests_are_maintenance`, `test_completion_caps_max_tokens_at_the_model_limit` |
| A cancel before the first stream event is charged the input estimate | `test_cancel_before_first_event_charges_the_input_estimate` |

## User settings (api.py `update_settings`) — `tests/test_settings.py`

| Behavior | Test |
|----------|------|
| settings() reflects the configuration and the provider list | `test_settings_reflect_the_configuration`, `test_settings_without_a_provider_list` |
| Model, maintenance model, prompt, budget, permissions apply from the next request | `test_live_settings_apply_from_the_next_request`, `test_maintenance_model_empty_string_clears_it` |
| Permissions stay clamped to the inherited ceiling | `test_permissions_stay_clamped_to_the_inherited_ceiling` |
| Raising a spent budget lets the agent continue | `test_raising_a_spent_budget_lets_the_agent_continue` |
| An invalid update changes nothing | `test_an_invalid_update_changes_nothing` |
| Provider switch rebuilds the Core; a provider that cannot list models is refused | `test_switching_provider_*`, `test_a_provider_that_cannot_list_models_is_not_switched_to` |
| Provider, key and work folder wait until nothing runs; work folder rebuilds Tools | `test_provider_and_work_folder_wait_until_nothing_runs`, `test_work_folder_rebuilds_the_tools` |
| Settings before start; the Core is asked for models only when needed | `test_settings_before_start`, `test_settings_before_start_need_the_provider_only_for_models` |
| Keys go to the keychain only | `test_api_key_*` |
| Only changed settings are saved; changes are logged without secrets, loosening flagged | `test_only_changed_settings_are_saved`, `test_settings_changes_are_logged_without_values_that_are_secret`, `test_raising_a_limit_and_loosening_permissions_are_flagged` |

## Launcher (`kayden`) — `tests/test_e2e.py`

| Behavior | Test |
|----------|------|
| Config file selects modules and runtime settings; flags override; invalid files refused | `test_config_file_in_the_data_dir_selects_the_core`, `test_runtime_settings_come_from_the_config_file`, `test_invalid_config_files_are_refused` |
| System prompt inline or from a file | `test_system_prompt_replaces_the_default`, `test_system_prompt_file_is_relative_to_the_config_file` |
| Provider list and the current provider | `test_providers_and_the_current_one` |
| Saved settings override the config file; invalid ones refused | `test_saved_settings_override_the_config_file`, `test_invalid_saved_settings_are_refused` |
| `--show-config` prints the effective config, which loads back unchanged | `test_show_config_prints_the_effective_config_without_building_modules`, `test_shown_config_loads_back_to_the_same_instance` |
| Module startup errors are one line; the `kayden` command runs main | `test_module_startup_errors_are_one_line`, `test_kayden_command_runs_main` |

## Settings screen (TUI) — `tests/test_tui_settings.py`

| Behavior | Test |
|----------|------|
| F2 or /settings shows the current settings; saving sends only what changed | `test_settings_screen_shows_the_current_settings`, `test_saving_sends_only_what_changed` |
| Loosening permissions or raising or removing the limit asks first; tightening does not | `test_raising_the_spending_limit_asks_first`, `test_removing_the_limit_and_loosening_permissions_ask_first`, `test_tightening_does_not_ask` |
| Fits an 80×24 terminal | `test_settings_screen_fits_a_small_terminal` |
