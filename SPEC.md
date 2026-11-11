Kayden v1 — MVP Specification
Status: Product spec with module contracts (Draft 3.0); implementation choices are recorded in .agents/decisions.md and open contract issues in .agents/contract-issues.md
Purpose: Define the product, runtime responsibilities, module boundaries, and observable behavior before delegating implementation to coding agents.
Repository: this repository is the v1 implementation; the previous Kayden repository is kept as a reference.

1. Product Vision
Kayden is a complete, modular AI agent application, not a general-purpose framework for developers to assemble their own agent runtimes.
It provides persistent conversations the user can start, continue, curate and delete. In every conversation the agent reasons, uses tools, ends its turn, and resumes by itself when background work it started finishes.
The user controls what the agent sees: fresh starts at any time, continuity when asked for, and long-term memory the user can see and change.
Users may run multiple independent Kayden instances concurrently as separate operating-system processes, with their own configurations, memory, and budgets.
A conversation can be given a duty: standing work it plans and carries on by itself until a person ends it (4.4).
These agents can collaborate using ordinary tools. Kayden does not implement a bespoke multi-agent organization framework: PHILOSOPHY.md describes the fleet manager (KFM) meant to be built on Kayden instances as a separate program, and duties are what a Kayden instance provides toward it.
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
Waking up when background work finishes.
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
A frontend may offer Tools modules of its own, offered after every other module's. The desktop app and the terminal app host sub-sessions this way: the agent hands a self-contained task to a child instance it starts headless, in its own data folder with the parent's settings, the conversation's work folder and a spending limit the agent sets; the child's report or question is the call's result. The frontend lists each conversation's children and opens one like a conversation of its own, through a RuntimeAPI over the child's channel: the user sees its work as it happens, talks to it and answers its permission prompts. Working children stop with the parent; their records keep them listed, and opening one that is not running starts it again on its conversation.
A headless frontend lets another program drive an instance: JSON-RPC 2.0 over the process's stdin and stdout, each request a runtime command, each runtime event a notification. Permission prompts and the agent's questions go to that program. Launch options can start an instance on a duty.
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
4. Conversations
4.1 Conversations
Every conversation behaves as a conventional AI conversation by default.
Users initiate inference.
Conversations have explicit boundaries; a user may have many and reopen any of them.
The runtime manages conversation flow and tool calls.
Conversation histories persist across application restarts.
Inference restarts without a user message only when background work the agent started has finished (4.2), or for a duty (4.4).
A new conversation is a fresh start: no messages and no summary, with long-term memory included. It is stored only with its first use (a message, an activation, a duty or a name); one left before that leaves nothing behind. "Continue from" starts a new conversation that knows where an earlier one left off: on its first maintenance, Memory writes its initial rolling summary from the earlier conversation's summary and recent messages, and the earlier conversation is unchanged.
Conversations get a title automatically from their first message unless the user sets one. The user can rename, archive (hide from the list and from cross-conversation recall) and delete conversations.
Each conversation has its own work folder, chosen when it starts (by default the folder set in the settings; "Continue from" keeps the earlier conversation's). The runtime hands it to the Tools whenever the conversation is opened, so reopening a conversation puts the agent back in the same folder.
4.2 Waking Up
There is one kind of conversation. The agent's instructions and tools are the same for a conversation's whole life.
Within a cycle the model may:
Continue reasoning.
Invoke tools.
Take permitted actions.
End its turn and be woken when its background work finishes.
The application remains running even when the model is idle.
The runtime does not impose an artificial maximum number of consecutive thinking cycles.
The agent may continue until:
It ends its turn.
The user stops it.
A failure stops execution.
Its configured budget is exhausted.
The model does not need a simulated biological sleep cycle.
A conversation also restarts when supported wake-up conditions occur: completion of a pending tool batch, since the agent started that work for the user and reports the outcome without being asked, and a duty's wake-ups (4.4).
No general-purpose event subscriptions or arbitrary hooks are required.
Stop clears any pending wake-up, a duty's included. Permissions and budgets apply to a woken cycle as to any other.
4.3 Processes
One process runs one agent with one open conversation. The user may open a different conversation only when the agent has no outstanding operations. To let one conversation work while chatting in another, run a second instance with its own data directory; the two do not share memory.
4.4 Duties
A person may give any conversation a duty at any time, change it, or end it: an instruction for standing work, either kept up until a person ends it or a goal that also ends when the agent declares it reached, with a report. The model cannot start a duty. A duty needs a cost budget.
The conversation keeps a plan: steps with a status (todo, doing, done, dropped), an optional due time and an optional note. The agent replaces it with a runtime built-in tool; the user edits it. Every request shows the duty's instruction and the whole plan, so compaction never loses them. The duty and plan are conversation metadata that Memory stores; "Continue from" copies neither. Ending a duty keeps the plan.
While the agent of a conversation with a duty is idle, it wakes on the first outstanding operation to terminate (not the whole batch), on a user message, and on its schedule: the earliest due open plan item, held between the duty's minimum and maximum interval after the cycle ends, or the maximum interval when nothing is due. External events reach it through tools that wait for them in the background. Each wake the runtime starts by itself begins with a notice giving the time and the cause; each is logged.
When it is blocked, the agent asks its supervisor for help with a runtime built-in tool: the schedule pauses until the user sends a message, activates the conversation or changes the duty, while finished operations still wake it. The question is shown to the user. Whoever drives the instance is its supervisor: today the user; in a fleet, the fleet manager routes the question to the unit's supervisor and up the ranks, to a human only when no unit can resolve it.
Stop, a failure, an exhausted budget and a restart pause the duty: it wakes on its own again only after the user sends a message or activates the conversation. Permissions apply as always; a call that would prompt waits for the user.
5. Idle and Stop
5.1 Idle
There is no tool for going idle: a model turn that ends without tool calls ends the cycle, in every conversation. (A reply followed by an idle call was returned by some models as hidden reasoning, so the user saw nothing.)
When a turn ends without tool calls:
The current inference cycle ends.
The agent enters Idle.
Conversation context remains available.
Pending asynchronous tools continue running.
The runtime waits for an eligible activation event.
If no tools are pending, the agent remains idle until user interaction, unless it has results it has not seen (see 5.2).
The runtime never re-prompts on its own, except on a duty's schedule (4.4). The agent is woken once all outstanding operations have terminated, including ones from earlier turns (with a duty, once the first has). Activating an agent whose transcript is empty or ends on an assistant message appends a short runtime notice as a user message, so a request never ends on an assistant message.
5.2 Batch Wake-Up
When the agent has outstanding operations belonging to a pending batch, the runtime waits until all operations reach a terminal outcome.
Batch completion reactivates the model.
After Stop, results are retained until user initiation.
The runtime does not need to invoke the model separately for every individual tool completion. A conversation with a duty is the exception: it wakes on the first completion, so a tool waiting for an outside event can wake it while other work runs (4.4).
The wake set is every operation outstanding when the cycle ends, including operations from earlier turns. Operations that exceed their timeout end as timed out, so they cannot block the wake-up forever. If results the model has not seen exist and nothing is outstanding, the next cycle starts immediately. This is checked when the agent goes idle.
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
If the model ends its turn while a batch is outstanding, Kayden waits for all operations outstanding then (section 5.2) to finish or otherwise terminate before issuing the batch-completion wake-up.
6.4 Tool Failures
Failed operations return errors rather than silently disappearing.
Unresponsive tools must have an appropriate timeout, cancellation, or failure-handling path.
A hanging operation must not indefinitely prevent the agent from recovering.
Healthy long-running operations should not be terminated unnecessarily.
6.5 Proposals
The agent may propose a tool call instead of making it, for work that is its own idea: the call is validated, shown to the user with the agent's reason, and returns at once, as an operation. Nothing runs until the user approves it; then exactly that call runs, without another permission prompt, and its result wakes the agent; a dismissal wakes it too. A pending proposal is left out of the batch wake-up. The agent can withdraw it; Stop and switching conversations withdraw it; it does not outlive the process.
6.5.1 Plan
Every conversation offers update_plan: for a task with several steps the agent writes its plan and marks steps doing and done as it works. Frontends show the plan as a checklist while a step is open, and do not show the update_plan calls as steps. With a duty, an open step that falls due wakes the agent.
6.6 Wrap-up card
When a turn leaves the user a decision, the agent ends it by calling wrap_up alone as its last step: a question, up to three options with its recommendation first, and optionally a short recap of what is done and not done. The call ends the turn without another model request. An option is sent as an ordinary user message when clicked; at most one option may instead carry a tool call, which is validated and pending like a proposal (6.5): clicking it runs exactly that call, and its result wakes the agent. A user message withdraws that pending call first, without a separate wake-up. ask_for_help may carry replies too. Frontends show the newest such call as a card while no user message follows it; the user may close it or type anything else instead.
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
Important durable information, kept by the Memory module as Claude Code keeps it: a folder of Markdown files per project (a conversation's work folder; one folder for conversations without one). MEMORY.md is the index, one line per memory; every other file holds one memory with a name, a one-line description and a type (user, feedback, project, reference) in its frontmatter. Every request's system prompt carries the memory rules and the project's MEMORY.md; the model opens a memory file when it is relevant, and writes, edits and deletes files with the memory tool the Memory module offers, while it works. Nothing is saved without the model calling that tool. The user can see and edit the files. Memory outlives the conversations of its project.
4. Permanent Verbatim Archive
Original conversation messages and tool interactions are retained for historical retrieval until the user deletes their conversation.
Information removed from active inference context is not deleted from the archive.
The archive is append-only. The one exception is deletion the user explicitly asks for: deleting a conversation erases its messages, summaries, search index entries and metadata, so that the erased content is not recoverable from Kayden's store. Long-term memory belongs to the project, not the conversation, and stays. Deletion cannot reach copies outside Kayden's data directory, such as backups, nor what a model provider has already received.
8.2 Shared Memory Foundation
All conversations use the same memory architecture, with configurable compaction policies, following Codex-inspired conversation persistence and context compaction.
Long-term memory is one folder per project, shared by the conversations in that project, while individual conversation histories remain distinct.
The user can see the open conversation's memory files and add, edit and delete them. Deleting a file does not delete the messages it came from.
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
Memory maintenance must never independently restart LLM inference after the agent has gone idle.
Consolidation may occur during an authorized inference cycle or be deferred until the next one.
8.8 Cross-Conversation Retrieval
The recall tool, offered by the Memory module, searches the current conversation's archive, or with scope "all" every conversation that is not archived. Each hit is one message; the read_around tool shows a message with the messages before and after it, so the agent can see what a hit was answering. Both are read-only and never merge conversation histories.
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
The model may be instructed to stop early to conserve resources, but this does not replace runtime budget enforcement.
Usage is persisted in the instance data directory and applies for the instance lifetime; it resets only by explicit user action.
Token budgets count input (cached tokens included) plus output. Cost budgets need per-model prices when the provider reports no cost; an instance configured with a cost budget and no price source refuses to start.
Duration counts wall-clock time while any inference or tool operation runs, background operations included, excluding permission waits and retry backoff.
Built-in tools do not count as tool calls. Budgets are checked before each inference and tool start. Tool calls that cannot start because a budget is exhausted are recorded as refused.
A hard budget exhausted mid-stream cancels the inference immediately, and the agent fails with a budget error.
Exhausting any dimension stops all chargeable execution, inference included. An exhausted duration budget also cancels running operations as Stop does; the agent then fails with a budget error once they have terminated or been abandoned. A budget belongs to the data directory, across all its conversations; usage lives in usage.json, which the user deletes, while no process runs there, to reset it. A model request that fails before streaming anything is not charged. start() and select_model refuse a model without prices when a cost budget is set.
10. Multi-Instance Operation
Multiple Kayden instances can run independently as separate operating-system processes.
Each instance has its own:
Runtime.
Configuration.
Memory and storage.
Conversation history.
Model selection.
Execution state.
Budgets.
A crash in one instance should not directly terminate other instances.
10.1 Instance Creation
Instances are launched through ordinary application or OS mechanisms.
An agent with authorized terminal access may launch another Kayden process. A program can launch and drive one through the headless frontend (section 3); the fleet manager in PHILOSOPHY.md is such a program, a separate project.
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
Each instance owns its data directory. The runtime holds an OS-level lock on it, taken before any stored data is read or changed and released at shutdown; a second process opening the same directory fails immediately with an error naming the holder, and touches none of its data.
A launched instance cannot be more permissive than its launcher. At startup the runtime exports its permission policy as a ceiling in its environment, and every instance clamps its own policy to the stricter of its configuration and any inherited ceiling. Strictness order: deny, ask, allow read-only, allow workspace, auto. The exported ceiling is the instance's effective policy, carried in the KAYDEN_PERMISSION_CEILING environment variable; tools that launch subprocesses must pass that variable through. Launched instances have their own independent budgets.
The ceiling is a guardrail against accidental or naive escalation, not a sandbox. It follows the trust model in section 3.2: an agent with arbitrary terminal access can defeat it.

11. Persistence
Kayden preserves conversation history, relevant agent state, summaries, and memory across application restarts.
Previous conversations remain available for reopening, with their context and memory.
In-flight tool processes are not required to survive application shutdown.
Interrupted operations must not be falsely recorded as successful.
A backgrounded operation's placeholder is recorded as backgrounded, never as successful. Its outcome is appended as soon as it terminates when no cycle is running, or at the next turn boundary.
After a restart, a conversation opens idle with its context and memory restored and no pending wake-up. It resumes only when the user sends a message or activates it, so a crash never silently re-executes a half-finished action. Operations that were in flight are recorded as interrupted and shown to the model on its next cycle.
12. MVP Acceptance Scenarios
Scenario A — Replaceable Modules
A developer replaces a Core, Memory, Tools, or Frontend implementation without modifying the runtime.
Scenario B — Conversation
A user starts a conversation, exchanges messages, invokes tools, closes Kayden, and reopens the preserved conversation.
Scenario C — Background Work
An agent reasons, uses tools, ends its turn, and continues when its background work finishes.
Scenario D — Long-Running Tools
A slow operation returns control to the model with its status. The model can inspect its progress and continue working.
Scenario E — Idle Batch Wake-Up
Several tools run concurrently.
The agent enters Idle.
Kayden wakes the agent after the entire outstanding batch reaches terminal outcomes.
After Stop, automatic inference does not restart.
Scenario F — Stop
The user presses Stop during active inference or tool execution.
Kayden requests cancellation, prevents further automatic activation, and preserves persistent state.
Scenario G — Restart Persistence
Kayden restarts and restores conversation history, memory, rolling summaries, and archived records.
Scenario H — Historical Recall
After repeated compactions, an agent can retrieve relevant original messages from its permanent archive.
Scenario I — Recall Across Conversations
An agent reads relevant information from the user's other conversations without changing them.
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
Scheduled wake-ups other than a duty's.
General-purpose event subscription systems.
Reimplementation of Codex's proprietary compaction mechanisms.
Mandatory vector databases.
Speculative extension systems.
Multimodal message content (images, audio); v1 messages are text and tool blocks, and new block types can be added later.
Persistent cross-conversation permission grants.
Several conversations running at once in one process.
Deleting single messages.
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
Resolved by the API design (section 16): the Core, Memory, Tools and Frontend contracts; the Runtime API exposed to frontends; the Memory-to-Core inference mechanism; streaming event representation; tool cancellation; permission model; module discovery (entry points, or import-path factories in the configuration).
The storage formats and numeric defaults that are now decided are recorded in .agents/decisions.md.
New decisions of this kind are recorded in the same log without reopening settled product requirements.

16. Module API Contracts (Draft 3.0)
Normative source: src/kayden/api.py (Python 3.12+, asyncio). Types and signatures live there, and so do the runtime guarantees in its module docstring (durability order, serialization, resume repair, validation, wrapping, turn boundary). This section records the rest of the behavior the code cannot express.

16.1 Principles
Modules are in-process Python objects. There is no wire protocol; a module may use subprocesses or sockets internally.
All APIs are async. Cancellation is asyncio cancellation.
Modules never call each other. The runtime is the only caller.
Installed modules are Python entry points in the groups kayden.cores, kayden.memories, kayden.tools and kayden.frontends; an entry point's name is the module's id and its target the factory. Instance configuration names each module by id (chosen in the app) or as an import path "package.module:factory". The factory receives its configuration and the instance paths and returns the module. An instance has exactly one module of each kind; combining several tool providers is the Tools module's job.
Contracts evolve additively: optional fields, methods, events and block types. Draft 3.0 is a breaking exception: operating modes and activation policies left the contracts, since every implementation of them lives in this repository.
v1 messages are text and tool blocks only. Images and audio can be added later as new block types.
One agent per process (section 4.3).

16.2 Core
The runtime retries TransientCoreError with backoff; Core never retries itself. CoreError is permanent. ContextOverflowError makes the runtime re-assemble with 90% of the assembled-context budget (not of the window) and retry once.
ToolCallDelta and UsageReport are optional. If a request is cancelled or fails before Completed, the runtime charges, per dimension, the larger of the last UsageReport and its own estimate; a failure before anything streamed is not charged.
After a retry, frontends discard deltas received since the last committed message.
Reasoning blocks record the model that produced them, and Core drops reasoning state from other models.
A turn that stops at the token limit is committed; tool calls whose arguments did not parse are answered with an error result and the cycle continues.
Tool calling is mandatory. Token counting is a fast local estimate; Core's exact count is optional and used only for calibration.
Requests made through Memory's Completion carry maintenance=True; turns never do. A Core may use different provider options for maintenance (e.g. reasoning off).

16.3 Memory
The archive is append-only, full-fidelity and schema-versioned. Blocks are persisted generically so new block types need no migration.
history returns the full transcript from the archive regardless of compaction, or the part before or after a message. The runtime uses it for reopening conversations and for resume repair.
assemble is read-only, performs no inference, and always fits the token budget. Its messages are a suffix that starts at a user message, never splits a call from its result, and includes the newest message. A single oversized message is truncated in the assembled view only.
maintain runs only inside an authorized inference cycle, so it can never restart the agent by itself. It also writes a seeded conversation's first summary and a title for an untitled conversation (light model; a failed title call changes nothing; a title that is set is never overwritten). It receives the current context window, a metered Completion, and returns whether it acted. The Completion raises BudgetExhausted or CoreError. A maintain failure becomes a failure event without changing the agent state. Archive writes and compaction state are atomic.
search backs recall and search_archive, limited to a set of conversations or none. Queries are plain text. Long-term memory is one set of notes per store, shared by every conversation. append is idempotent by message id. Blocks are stored under stable type tags; unknown tags come back as opaque blocks. inspect is optional. A Memory module may offer tools of its own (recall, read_note, save_note and forget_note come with the built-in SQLite Memory); they run without prompts and outside the tool-call budget, since they touch only Kayden's memory. update_conversation changes conversation metadata (title, archived), which is not part of the append-only archive; save_duty, optional, stores a conversation's duty and plan the same way. Editing long-term memory (notes, save_note, delete_note) and delete_conversation are optional; the runtime reports a module without them, and frontends hide those controls. Erasure must leave no copy of the erased content in the store's files.

16.4 Tools
One coroutine per call. The runtime owns backgrounding, polling and cancellation; tools only handle cancellation promptly. The call id is the operation id.
A backgrounded call gets a placeholder result with status backgrounded; its outcome is recorded later as an operation result.
Consecutive read-only calls in a turn run concurrently. A non-read-only call is a barrier: it starts once every earlier call has finished or been backgrounded, and later calls wait for it likewise.
Tools never block the event loop. Blocking work goes through threads or subprocesses.
stall_timeout (silence) and timeout (wall clock) are optional per tool. Operations also end as timed out at the runtime-wide default timeout (suggested 1 hour; a tool may declare a longer one or none), so a hung operation cannot block a wake-up forever. A deliberate long-runner keeps the agent waiting until it ends or the user writes.
Every tool the runtime offers takes an optional step_summary argument: a few plain words the model writes about the call, which frontends show in place of the tool and its input. The runtime adds it to each schema and removes it before the Tools module sees the call; the transcript keeps it. The runtime's own tools (op_status, op_cancel, propose, suggest_replies, and with a duty or plan update_plan, ask_for_help and finish_goal) have schemas defined in api.py, are never gated by permissions, and cannot be redeclared by modules. Several Tools modules may run at once; MCP servers the user adds in the settings run as one more (each a command Kayden starts or a URL, their tools named mcp__<server>__<tool>, never confined, started with the next start, their variable values kept in the keychain). A Core may offer tools its provider runs during the request (Claude's web search); the runtime never runs, gates or counts them, and each use is recorded in the answer. Tools are offered in order: the runtime's, then Memory's, then the Core's, then each Tools module's; a name already offered is dropped from a later module, and each call goes to the module that offered it.
Results are plain strings. Frontends may parse formats they know.
Tools that launch subprocesses pass the permission ceiling variable through unmodified; they may leave out other variables, such as secrets from sandboxed commands.
use_folder is optional: the runtime calls it whenever it opens a conversation, with that conversation's work folder, before any of its calls. A module without it has one fixed folder, and conversations cannot choose another.

16.5 Frontend and Runtime API
A Frontend implements run(runtime) and declares whether it can answer permission prompts.
The valid commands per state are tabulated in api.py. The user can also cancel a single running operation. The runtime appends exactly one tool message per turn, directly after the assistant message. start may be called again only when no operations are outstanding.
Recovery for a late-attaching frontend: subscribe, take the snapshot, drop events at or below its sequence number, load history, and de-duplicate by message id. Subscriber queues are bounded.
send during a running cycle or while stopping queues the message until the next turn boundary. A queued message is held in memory until it is appended to the transcript, at the next turn boundary, when the agent reaches Stopped, or at graceful shutdown; a crash before then loses it. MessageQueued is not a durability promise: only MessageCommitted is.
Frontends read and change user settings through settings() and update_settings(), documented in api.py. Runtime settings, which the runtime reads while running (model, maintenance model, budget, permissions, system prompt, the folder for new conversations), apply live. Module settings, which are only passed to module factories (which module of each kind, the API key), are saved at once and apply at the next start; restart() starts the instance again with them, and Restart now may bring up a frontend of another kind (terminal to window) where it can. Other module options stay in the configuration file. When the modules cannot be built, the frontend still runs and shows the settings with the error. Frontends must ask the user to confirm before loosening permissions or raising a budget limit, and before switching Memory. The model is selectable at any time. Conversations can be renamed, archived and deleted, and long-term memory notes added, edited and deleted, in any state; each change emits an event. Deleting the open conversation is refused, and frontends must ask the user to confirm a deletion, saying it cannot be undone.
Permission policy is auto (the default), allow workspace, allow read-only, ask, or deny, with the exact meaning documented in api.py. Under allow workspace, calls the Tools module reports as confined to its work folder run without a prompt and every other call prompts; a Tools module that cannot tell confines nothing. Under auto, confined calls run too, and every other call is first reviewed by one metered request to the maintenance model that sees only the text of the user's messages and the call; a clear allow runs it, anything else (a failed review included) prompts with the reviewer's reason. The built-in Tools confine file calls by path and shell commands with a sandbox (bubblewrap) in which only the work folder is writable and home directories are hidden; a command the model marks unsandboxed prompts. Their web search and page reading touch nothing on the computer and are confined; page reading refuses local and private network addresses. A work folder that is or contains the home directory confines nothing. Grants are once or per conversation, in memory only; a conversation grant given for a confined call covers that tool's confined calls only, one given for a call that can reach outside covers all of its calls. Calls that need no prompt start immediately; while any prompt is open the state is awaiting permission and other operations keep running. Denied calls get a refused result. Resolution is first-wins. A barrier call's permission prompt opens when its turn comes, so prompts for mutating calls open one at a time in call order.
A frontend that cannot answer prompts is never sent any; calls that would prompt are refused.

16.6 Runtime behavior
Model failures are retried with backoff and never trigger a silent model switch.
Budget semantics are in section 9.
Each instance writes a JSONL event log of runtime decisions to its data directory (events.jsonl), in addition to the event stream: every event except deltas and progress, without content (messages, tool calls and results are recorded by id, kind and size, never by text, arguments or output), plus decision records (budget checks, permission outcomes, stop, backgrounding, abandonment, late completions, retries, idle and wake-up, cycle start and end, resume repair).
Graceful shutdown cancels running calls. Only a crash produces interrupted results.

Final Product Definition
Kayden is a modular AI agent application whose runtime provides complete conversation orchestration.
Core provides model intelligence.
Memory provides continuity.
Tools provide executable capabilities.
Frontend provides the user experience.
Independent Kayden instances can operate and collaborate using ordinary tools.
Kayden's value lies in being a useful, reliable AI agent—not in requiring developers to construct their own agent runtime.