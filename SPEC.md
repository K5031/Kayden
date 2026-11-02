Kayden v1 — MVP Specification
Status: Product spec with module contracts (Draft 2.4); implementation choices are recorded in docs/decisions.md and open contract issues in docs/contract-issues.md
Purpose: Define the product, runtime responsibilities, module boundaries, and observable behavior before delegating implementation to coding agents.
Repository: this repository is the v1 implementation; the previous Kayden repository is kept as a reference.

1. Product Vision
Kayden is a complete, modular AI agent application, not a general-purpose framework for developers to assemble their own agent runtimes.
It supports two operating modes:
Session Mode: Conventional, user-initiated conversations with persistent histories.
Continuous Mode: A persistent agent that can continue reasoning, use tools, voluntarily become idle, and resume under a user-selected activation policy.
Users may run multiple independent Kayden instances concurrently as separate operating-system processes, with their own configurations, memory, and budgets.
These agents can collaborate using ordinary tools. Kayden does not implement a bespoke multi-agent organization framework.
Core principle: The runtime owns orchestration; modules provide capabilities; frontends control presentation.
Engineering priorities:
User-visible utility.
Clear developer experience.
Reliable behavior.
Minimal necessary abstractions.
Product requirements before architecture.
Proven solutions over speculative engineering.
2. Runtime and Module Architecture
The Runtime is Kayden's built-in, non-replaceable agent orchestrator.
It owns:
Conversation orchestration.
Session and continuous operating modes.
Model and tool scheduling.
Agent lifecycle and activation.
Execution state and cancellation.
Budget enforcement.
Module coordination.
2.1 Four Module APIs
Kayden v1 has four Python module APIs.
Module
Responsibility
Core
Model discovery, inference, streaming responses, and model-generated tool calls
Memory
Transcripts, context assembly, compaction, archives, retrieval, consolidation, and forgetting
Tools
Tool declarations, execution, parameters, results, status, and cancellation
Frontend
User input, output, controls, permissions UI, status display, and presentation

Core is Kayden's model component. It is not a separate orchestration system.
There are no separate Conversation, Input, Output, Budget, or Storage modules.
2.2 Module Development
Module developers implement predefined Python API contracts.
They may choose their internal technologies, including React, Qt, subprocesses, or other languages.
The runtime already knows how supported module APIs interact.
Kayden does not require:
Arbitrary module wiring.
Generic contribution graphs.
Universal lifecycle hooks.
Replaceable orchestration runtimes.
Only information necessary for runtime integration belongs in Kayden's module APIs.
APIs may be sophisticated when actual requirements justify it.
Detailed Python signatures and data structures are defined in section 16 and src/kayden/api.py.
3. Frontend Architecture
Frontends may be terminal interfaces, desktop applications, web applications, or other interaction systems.
Frontend developers control:
Appearance and layout.
Placement and visibility of controls.
Navigation and interaction patterns.
UI frameworks.
Rendering and presentation.
The frontend communicates with Kayden through its supported runtime API.
It can submit messages, access conversation information, select models, receive execution updates, present permission requests, and control agent execution.
3.1 Tool Presentation
Tools exist primarily for the model, not for the user interface.
Tool developers provide functionality and metadata such as names, descriptions, and parameter schemas.
They do not need to develop custom interfaces.
Frontends may optionally display tool activity and results through their own presentation mechanisms.
Kayden does not implement a generic UI framework or require tool-specific React components.
3.2 Trust Model
Installed third-party modules are trusted software.
A malicious frontend could misrepresent user actions or permission decisions.
Python API boundaries do not inherently provide security isolation.
The runtime should still enforce applicable permissions and execution policies.
4. Operating Modes
4.1 Session Mode
Session Mode provides conventional AI conversation behavior.
Users initiate inference.
Conversations have explicit session boundaries.
The runtime manages conversation flow and tool calls.
Conversation histories persist across application restarts.
Inference does not autonomously restart between user-authorized turns.
4.2 Continuous Mode
Continuous Mode provides persistent agents capable of ongoing autonomous activity.
The model may:
Continue reasoning.
Invoke tools.
Take permitted actions.
Voluntarily enter Idle.
The application remains running even when the model is idle.
The runtime does not impose an artificial maximum number of consecutive thinking cycles.
The agent may continue until:
It voluntarily idles.
The user stops it.
A failure stops execution.
Its configured budget is exhausted.
The model does not need a simulated biological sleep cycle.
4.3 Activation Policies
Continuous Mode supports two policies.
User-Controlled
Only the user may initiate another inference cycle after the model goes idle.
Completed background tool results remain available until the user initiates another cycle.
Agent-Controlled
The model may continue inference and automatically resume when supported wake-up conditions occur.
For v1, completion of a pending tool batch is the only required autonomous wake-up trigger.
No general-purpose event subscriptions, scheduled wake-ups, or arbitrary hooks are required.
4.4 Mode Selection
Users select Session Mode or Continuous Mode before starting an agent.
Live switching between operating modes is not supported in v1.
One process runs one agent. An instance keeps a separate store per mode; to use both at once, run two processes (the data-directory lock is per mode). A Continuous instance has exactly one continuous conversation, which starting resumes, creating it if absent. Session Mode may open a different conversation only when the agent has no outstanding operations.
5. Idle and Stop
5.1 Idle Tool
Continuous Mode exposes a built-in idle() tool.
When invoked:
The current inference cycle ends.
The agent enters Idle.
Conversation context remains available.
Pending asynchronous tools continue running.
The runtime waits for an eligible activation event.
If no tools are pending, the agent remains idle until user interaction, unless it is Agent-Controlled and has results it has not seen (see 5.2).
A model turn that ends without tool calls is treated as an implicit idle(); the runtime never re-prompts on its own. idle() may name the operations to wait for; by default it waits for all outstanding ones, including operations from earlier turns. Activating an agent whose transcript is empty or ends on an assistant message appends a short runtime notice as a user message, so a request never ends on an assistant message.
5.2 Batch Wake-Up
When the agent has outstanding operations belonging to a pending batch, the runtime waits until all operations reach a terminal outcome.
Under Agent-Controlled activation, batch completion may automatically reactivate the model.
Under User-Controlled activation, results are retained until user initiation.
The runtime does not need to invoke the model separately for every individual tool completion.
The wake set is every operation outstanding at the end of the turn that contains idle(), including operations from earlier turns; unknown ids in wait_for are ignored. Operations that exceed their timeout end as timed out, so they cannot block the wake-up forever. If results the model has not seen exist and nothing is outstanding, an Agent-Controlled agent starts the next cycle immediately and a User-Controlled agent retains them. This is checked when the agent goes idle; operations that complete later but are outside the wake set do not wake it.
5.3 Stop Control
Kayden provides one user-facing Stop action.
There is no separate Pause control in v1.
When Stop is requested:
Stop initiating new inference and tool operations.
Request cancellation of active inference.
Request cancellation of running tools where possible.
Disable automatic wake-ups.
Preserve conversation and memory state.
Report when outstanding operations have terminated.
The frontend may display Stopping… while cancellation proceeds, without freezing its interface.
If an operation cannot terminate promptly, Kayden must report the problem instead of hanging silently.
After a grace period, calls that have not confirmed cancellation are abandoned: they are reported, recorded with an abandoned result stating they may still have effects, and the agent moves to Stopped. A late completion is logged and never appended. Messages sent while stopping are queued and start a cycle once stopped; messages queued before Stop are appended to the transcript when the agent reaches Stopped, and start nothing. Stop with nothing outstanding in Idle, Stopped or Failed is a no-op; with operations outstanding it performs a full Stop.
Previously completed external actions may not be reversible.
6. Tool Execution
Kayden follows Codex-inspired long-running execution behavior.
6.1 Short Operations
Short-running tools return their results normally.
6.2 Long-Running Operations
When an operation exceeds a configurable initial waiting period:
The runtime may return control to the model.
The operation continues running.
The model receives an operation identifier and available progress information.
The model may inspect progress, poll again, perform other work, or request cancellation.
This gives the model the ability to investigate slow operations without requiring every tool to finish before inference can continue.
6.3 Tool Batches
Multiple independent tool calls may execute concurrently.
If the model calls idle() while a batch is outstanding, Kayden waits for all operations outstanding at the end of that turn (section 5.2) to finish or otherwise terminate before issuing the batch-completion wake-up.
6.4 Tool Failures
Failed operations return errors rather than silently disappearing.
Unresponsive tools must have an appropriate timeout, cancellation, or failure-handling path.
A hanging operation must not indefinitely prevent the agent from recovering.
Healthy long-running operations should not be terminated unnecessarily.
7. Model Failures
For transient provider failures, Kayden:
Retries a small configurable number of times.
Uses backoff between retries.
Stops execution if retries continue failing.
Reports the failure to the user.
Preserves conversation context and memory.
Kayden must not silently switch to another model.
Tool calling is mandatory for Core implementations in v1.
No tool-less fallback is required.
8. Memory Architecture
The Memory module owns the entire memory lifecycle.
This includes:
Active conversation transcripts.
Context assembly.
Context compaction.
Rolling summaries.
Long-term memory.
Permanent historical archives.
Historical retrieval.
Memory consolidation.
Forgetting policies.
The runtime requests inference context from Memory and forwards new conversation and tool events to it.
Memory implementation details remain internal to the Memory module.
8.1 Memory Layers
Kayden uses four memory components.
1. Recent Verbatim Context
Recent conversation messages and tool interactions preserved in their original form for inference.
2. Rolling Summary
A compact representation of older conversation history.
The summary has a fixed token budget and is automatically carried into subsequent inference requests.
3. Long-Term Memory
Important durable information maintained by the Memory module.
4. Permanent Verbatim Archive
Original conversation messages and tool interactions are permanently retained for historical retrieval.
Information removed from active inference context is not deleted from the archive.
8.2 Shared Memory Foundation
Session and Continuous modes use the same underlying memory architecture, with configurable compaction policies.
Session Mode follows Codex-inspired conversation persistence and context compaction.
Continuous Mode uses the same foundations across an ongoing agent lifetime.
In Session Mode, long-term memory can preserve reusable information across separate sessions, while individual session histories remain distinct.
8.3 Compaction
When active context reaches its configured threshold:
Select an older portion of active messages at message boundaries.
Combine those messages with the previous rolling summary.
Generate an updated summary within the fixed token budget.
Preserve the original messages permanently in the historical archive.
Remove the selected messages from active inference context.
Retain recent messages verbatim.
Carry the updated summary into subsequent inference requests.
8.4 Cut-Half and Gradual Decay
The original Cut-Half configuration compacts approximately 50% of active conversation context while retaining the newer portion.
The compaction trigger threshold and the percentage removed are separate configurable parameters.
Repeated rewriting of a fixed-budget summary can produce gradual decay:
Important information may persist.
Less relevant information may disappear from the active summary.
All original historical messages remain archived.
Cut-Half and Gradual Decay are therefore treated as configurations or behaviors of one general compaction architecture, not entirely separate systems.
8.5 Historical Retrieval
The rolling summary may serve as a guide to historical retrieval.
However, summary contents alone are insufficient for reliable search because information may disappear during compaction.
The historical archive must remain independently searchable.
Possible implementation mechanisms include identifiers, timestamps, and text indexing.
The exact indexing technology is not prescribed.
8.6 Reliability
Archiving and compaction-state updates must be crash-safe.
The system must not remove the only preserved copy of historical information during compaction.
Memory quality should be tested across repeated consolidation cycles.
8.7 Model-Assisted Consolidation
Memory decides when, what, and how to consolidate.
It may need LLM inference for consolidation.
Memory receives a metered one-shot inference callable from the runtime (section 16.3), so it never calls Core directly.
The chosen mechanism must:
Preserve Memory's responsibility for memory management.
Respect runtime-enforced budgets.
Avoid unnecessary context forwarding.
Avoid cyclic orchestration dependencies.
Under User-Controlled activation, memory maintenance must not independently restart LLM inference after the agent has gone idle.
Consolidation may occur during an authorized inference cycle or be deferred until the next one.
8.8 Operating-Mode Isolation
Session Mode and Continuous Mode maintain separate conversation histories and memory contexts within an instance.
A read-only tool, conceptually similar to peek_other_mode(query), permits retrieval of relevant information from the other mode.
It must not:
Modify the other mode's memory.
Wake or interrupt the other agent.
Automatically merge their conversation histories.
9. Budgets
Budget management belongs to the runtime.
Each Kayden instance has independent configuration and resource limits.
Possible budget dimensions include:
Token consumption.
API cost.
Execution duration.
Tool calls.
Model providers report usage when available.
Frontends may display configuration and usage information, but do not enforce budgets.
Once a configured hard budget is exhausted, the runtime must prevent further chargeable execution and report the reason.
The model may be instructed to call idle() to conserve resources, but this does not replace runtime budget enforcement.
Usage is persisted in the instance data directory and applies for the instance lifetime; it resets only by explicit user action.
Token budgets count input (cached tokens included) plus output. Cost budgets need per-model prices when the provider reports no cost; an instance configured with a cost budget and no price source refuses to start.
Duration counts wall-clock time while any inference or tool operation runs, background operations included, excluding permission waits and retry backoff.
Built-in tools do not count as tool calls. Budgets are checked before each inference and tool start. Tool calls that cannot start because a budget is exhausted are recorded as refused.
A hard budget exhausted mid-stream cancels the inference immediately, and the agent fails with a budget error.
Exhausting any dimension stops all chargeable execution, inference included. An exhausted duration budget also cancels running operations as Stop does; the agent then fails with a budget error once they have terminated or been abandoned. A budget belongs to the (directory, mode); usage lives in usage-<mode>.json, which the user deletes, while no process runs that mode, to reset it. A model request that fails before streaming anything is not charged. start() and select_model refuse a model without prices when a cost budget is set.
10. Multi-Instance Operation
Multiple Kayden instances can run independently as separate operating-system processes.
Each instance has its own:
Runtime.
Configuration.
Memory and storage.
Conversation history.
Model selection.
Operating mode.
Execution state.
Budgets.
A crash in one instance should not directly terminate other instances.
10.1 Instance Creation
Instances are launched through ordinary application or OS mechanisms.
An agent with authorized terminal access may launch another Kayden process.
No dedicated agent-spawning API is required.
10.2 Instance Communication
Agents communicate through ordinary available tools and external services.
Kayden does not need:
Dedicated agent-to-agent protocols.
Shared memory databases.
Centralized fleet management.
Hardcoded organizational hierarchy.
Runtime-specific multi-agent orchestration.
10.3 Workforce Scenario
A user may deploy independent Kayden instances with different responsibilities, such as:
Research lead.
Research assistant.
Software engineer.
Routine task assistant.
Organizational roles and ranks are not hardcoded into Kayden.
Capabilities and authority depend on each instance's configuration, model, tools, and permissions.
10.4 Instance Safety
Each instance owns its data directory per mode. The runtime holds an OS-level lock per (directory, mode); a second process opening the same mode fails immediately with an error naming the holder. Two processes may share a directory if they run different modes.
A launched instance cannot be more permissive than its launcher. At startup the runtime exports its permission policy as a ceiling in its environment, and every instance clamps its own policy to the stricter of its configuration and any inherited ceiling. Strictness order: deny, ask, allow read-only. The exported ceiling is the instance's effective policy, carried in the KAYDEN_PERMISSION_CEILING environment variable; tools that launch subprocesses must pass the environment through. Launched instances have their own independent budgets.
This is a guardrail against accidental or naive escalation, not a sandbox. It follows the trust model in section 3.2: an agent with arbitrary terminal access can defeat it.

11. Persistence
Kayden preserves conversation history, relevant agent state, summaries, and memory across application restarts.
Session Mode
Previous conversations remain available for reopening.
Continuous Mode
The agent resumes with its previous context and memory rather than starting a new conversation from scratch.
In-flight tool processes are not required to survive application shutdown.
Interrupted operations must not be falsely recorded as successful.
A backgrounded operation's placeholder is recorded as backgrounded, never as successful. Its outcome is appended as soon as it terminates when no cycle is running, or at the next turn boundary.
After a restart, a Continuous agent comes back stopped, with its context and memory restored. It resumes only when the user activates it, regardless of activation policy, so a crash never silently re-executes a half-finished action. Operations that were in flight are recorded as interrupted and shown to the model on its next cycle.
12. MVP Acceptance Scenarios
Scenario A — Replaceable Modules
A developer replaces a Core, Memory, Tools, or Frontend implementation without modifying the runtime.
Scenario B — Session Conversation
A user starts a session, exchanges messages, invokes tools, closes Kayden, and reopens the preserved conversation.
Scenario C — Continuous Autonomy
A continuous agent reasons, uses tools, decides to idle, and continues according to its activation policy.
Scenario D — Long-Running Tools
A slow operation returns control to the model with its status. The model can inspect its progress and continue working.
Scenario E — Idle Batch Wake-Up
Several tools run concurrently.
The agent enters Idle.
Under Agent-Controlled activation, Kayden wakes after the entire outstanding batch reaches terminal outcomes.
Under User-Controlled activation, automatic inference does not restart.
Scenario F — Stop
The user presses Stop during active inference or tool execution.
Kayden requests cancellation, prevents further autonomous activation, and preserves persistent state.
Scenario G — Restart Persistence
Kayden restarts and restores conversation history, memory, rolling summaries, and archived records.
Scenario H — Historical Recall
After repeated compactions, an agent can retrieve relevant original messages from its permanent archive.
Scenario I — Cross-Mode Memory Access
An agent reads relevant information from the other operating mode without changing that mode's state.
Scenario J — Multi-Instance Workforce
Three independently running Kayden instances have separate memory, configuration, and budgets.
They coordinate work through ordinary tools.
Scenario K — Model Failure
Transient provider failures trigger limited retries.
Persistent failures terminate execution with a visible error, without automatically switching models.
Scenario L — Budget Enforcement
When a hard budget is exhausted, the runtime prevents further chargeable execution.
13. Explicitly Out of Scope
The following are not required for Kayden v1:
Replaceable agent runtimes.
Arbitrary module-composition frameworks.
Generic contribution meshes.
Mandatory plugin lifecycle hooks.
Cross-language module APIs.
Generic UI frameworks.
Tool-specific custom UI components.
Module marketplace and hosted distribution.
Cloud synchronization.
Centralized fleet management.
Special multi-agent messaging infrastructure.
Scheduled autonomous wake-ups.
General-purpose event subscription systems.
Reimplementation of Codex's proprietary compaction mechanisms.
Mandatory vector databases.
Speculative extension systems.
Multimodal message content (images, audio); v1 messages are text and tool blocks, and new block types can be added later.
Conversation deletion and renaming.
Persistent cross-conversation permission grants.
Running both operating modes in one process.
14. Implementation Strategy
The implementer may determine:
Idiomatic Python API signatures.
Internal classes and data structures.
Storage and indexing technologies.
Streaming event formats.
Asynchronous task representations.
Retry and timeout defaults.
Cancellation implementation.
These choices must remain consistent with the agreed product behavior and architectural boundaries.
Development should proceed through bounded, testable vertical slices.
Engineering Rules
Define expected behavior before implementation.
Avoid speculative abstractions.
Keep the runtime responsible for orchestration.
Preserve developer freedom within module APIs.
Prefer established libraries and proven mechanisms.
Avoid unnecessary refactoring.
Do not introduce features outside the agreed MVP.
Keep interfaces documented and independently implementable.
Test end-to-end workflows.
Prioritize useful product progress over architectural sophistication.
15. Remaining Design Work
Resolved by the API design (section 16): the Core, Memory, Tools and Frontend contracts; the Runtime API exposed to frontends; the Memory-to-Core inference mechanism; streaming event representation; tool cancellation; permission model; module discovery (import-path factories).
The storage formats and numeric defaults that are now decided are recorded in docs/decisions.md.
New decisions of this kind are recorded in the same log without reopening settled product requirements.

16. Module API Contracts (Draft 2.4)
Normative source: src/kayden/api.py (Python 3.12+, asyncio). Types and signatures live there, and so do the runtime guarantees in its module docstring (durability order, serialization, resume repair, validation, wrapping, turn boundary). This section records the rest of the behavior the code cannot express.

16.1 Principles
Modules are in-process Python objects. There is no wire protocol; a module may use subprocesses or sockets internally.
All APIs are async. Cancellation is asyncio cancellation.
Modules never call each other. The runtime is the only caller.
Instance configuration lists each module as an import path "package.module:factory". The factory receives its configuration and the instance paths and returns the module.
Contracts evolve additively: optional fields, methods, events and block types.
v1 messages are text and tool blocks only. Images and audio can be added later as new block types.
One agent per process (section 4.4).

16.2 Core
The runtime retries TransientCoreError with backoff; Core never retries itself. CoreError is permanent. ContextOverflowError makes the runtime re-assemble with a 10% smaller budget and retry once.
ToolCallDelta and UsageReport are optional. If a request is cancelled or fails before Completed, the runtime charges, per dimension, the larger of the last UsageReport and its own estimate; a failure before anything streamed is not charged.
After a retry, frontends discard deltas received since the last committed message.
Reasoning blocks record the model that produced them, and Core drops reasoning state from other models.
A turn that stops at the token limit is committed; tool calls whose arguments did not parse are answered with an error result and the cycle continues.
Tool calling is mandatory. Token counting is a fast local estimate; Core's exact count is optional and used only for calibration.
Requests made through Memory's Completion carry maintenance=True; turns never do. A Core may use different provider options for maintenance (e.g. reasoning off).

16.3 Memory
The archive is append-only, full-fidelity and schema-versioned. Blocks are persisted generically so new block types need no migration.
history returns the full transcript from the archive regardless of compaction. The runtime uses it for reopening conversations and for resume repair.
assemble is read-only, performs no inference, and always fits the token budget. Its messages are a suffix that starts at a user message, never splits a call from its result, and includes the newest message. A single oversized message is truncated in the assembled view only.
maintain runs only inside an authorized inference cycle, so it can never restart a User-Controlled agent. It receives the current context window, a metered Completion, and returns whether it acted. The Completion raises BudgetExhausted or CoreError. A maintain failure becomes a failure event without changing the agent state. Archive writes and compaction state are atomic.
search backs recall (current conversation) and peek_other_mode (the other mode's archive, read-only). Queries are plain text. Long-term memory is per mode, shared by all conversations of that mode. append is idempotent by message id. Blocks are stored under stable type tags; unknown tags come back as opaque blocks. inspect is optional. The store must tolerate a second process (the other mode) reading it for peek_other_mode.

16.4 Tools
One coroutine per call. The runtime owns backgrounding, polling and cancellation; tools only handle cancellation promptly. The call id is the operation id.
A backgrounded call gets a placeholder result with status backgrounded; its outcome is recorded later as an operation result.
Consecutive read-only calls in a turn run concurrently. A non-read-only call is a barrier: it starts once every earlier call has finished or been backgrounded, and later calls wait for it likewise.
Tools never block the event loop. Blocking work goes through threads or subprocesses.
stall_timeout (silence) and timeout (wall clock) are optional per tool. Operations also end as timed out at the runtime-wide default timeout (suggested 1 hour; a tool may declare a longer one or none), so a hung operation cannot block a wake-up forever. The model can exclude a deliberate long-runner with idle(wait_for).
The built-in tools (idle, op_status, op_cancel, recall, peek_other_mode) have schemas defined in api.py, are never gated by permissions, and cannot be redeclared by modules.
Results are plain strings. Frontends may parse formats they know.
Tools that launch subprocesses pass the environment through unmodified.

16.5 Frontend and Runtime API
A Frontend implements run(runtime) and declares whether it can answer permission prompts.
The valid commands per state are tabulated in api.py. The user can also cancel a single running operation. The runtime appends exactly one tool message per turn, directly after the assistant message. start may be called again only when no operations are outstanding.
Recovery for a late-attaching frontend: subscribe, take the snapshot, drop events at or below its sequence number, load history, and de-duplicate by message id. Subscriber queues are bounded.
send during a running cycle or while stopping queues the message until the next turn boundary. A queued message is held in memory until it is appended to the transcript, at the next turn boundary, when the agent reaches Stopped, or at graceful shutdown; a crash before then loses it. MessageQueued is not a durability promise: only MessageCommitted is.
Configuration is read-only to frontends; a later start() may change the activation policy but never the mode. The model is selectable at any time. A Continuous start with a conversation id other than the instance's single continuous conversation is refused.
Permission policy is ask, allow read-only, or deny, with the exact meaning documented in api.py. Grants are once or per conversation, in memory only. Calls that need no prompt start immediately; while any prompt is open the state is awaiting permission and other operations keep running. Denied calls get a refused result. Resolution is first-wins. A barrier call's permission prompt opens when its turn comes, so prompts for mutating calls open one at a time in call order.
A frontend that cannot answer prompts is never sent any; calls that would prompt are refused.
v1 has no conversation deletion or renaming.

16.6 Runtime behavior
Model failures are retried with backoff and never trigger a silent model switch.
Budget semantics are in section 9.
Each instance writes a JSONL event log of runtime decisions to its data directory, in addition to the event stream.
Graceful shutdown cancels running calls. Only a crash produces interrupted results.

Final Product Definition
Kayden is a modular AI agent application whose runtime provides complete session-based and continuous orchestration.
Core provides model intelligence.
Memory provides continuity.
Tools provide executable capabilities.
Frontend provides the user experience.
Independent Kayden instances can operate and collaborate using ordinary tools.
Kayden's value lies in being a useful, reliable AI agent—not in requiring developers to construct their own agent runtime.