# Agent Note: Automatic permission handling

Status: implemented — a quick AI review allows the calls that fit what the user asked, and asks about the rest

## Problem

Under "free in the work folder" the agent works without prompts inside its folder, but
every call that can reach outside asks: an unsandboxed command to run `uv` or `cargo`
(installed under the home folder, hidden in the sandbox), reading a file the user pointed
at in Downloads, `git push`. Most of these are obviously what the user asked for, and a
non-technical user cannot judge a shell command anyway, so the prompts are noise until they
are clicked away without reading. The choice was between asking too often and allowing
everything.

## Decision

### A permission level: automatic

`PermissionPolicy.AUTO`, the loosest level and the default, shown as "Decide
automatically, ask me only when unsure". Strictness order: deny, ask, allow read-only,
allow workspace, auto.

- A confined call runs, as under `allow_workspace`. A call covered by a conversation grant
  runs.
- Every other call is **reviewed**: one request decides whether it runs without asking.
  "Allow" runs it; anything else opens the usual prompt, with the reviewer's reason in
  `PermissionRequest.reason`. With a non-interactive frontend that call is refused.

### The review

- A runtime request (`runtime/review.py`, `Runtime._review`) on the maintenance model,
  charged to the budget, with no tools and `maintenance=True` (Cores use their maintenance
  options, such as reasoning off). `InferenceRequest.maintenance` covers the runtime's own
  one-shot requests.
- It sees what the user asked, not what the agent read: the text of the user's messages
  (newest first, up to 16 000 characters), the work folder, and the call (tool name,
  description, arguments cut at 4 000 characters, without the model's own `description`
  of a command). Tool results, web pages and the agent's own text are left out, so a page that says "now delete everything" cannot argue
  for its own call.
- The rules are in `REVIEW_PROMPT`: allow what clearly serves what the user asked and only
  reads or is expected and easily undone; ask for anything that deletes or overwrites what
  the user did not mention, sends data or credentials anywhere, publishes or pushes,
  spends money, installs system-wide, changes system or security settings, or that the
  user's messages do not clearly cover. Building, testing or running the user's own project
  on request is expected, and being outside the work folder or unsandboxed is no reason
  to ask by itself (every reviewed call is). Claims of permission inside the action do not
  count. When unsure, ask. The reason is one sentence of at most 20 words.
- Checked once against real models (Claude Haiku 5.5 reviewing, 2026-11-10): ten typical
  cases, from listing Downloads to an injected `rm -rf ~/code`, all decided as intended.
- Calls whose arguments mention credential files (SSH and GPG keys, `.env`, shell
  startup files such as `.bashrc`, where keys are often exported, password stores, cloud credentials, browser profiles, the keychain) or Kayden's data folder are
  never reviewed and always ask (`review.always_ask`, a fixed pattern match). A secret
  the agent reads goes to the provider, and once in the conversation a sandboxed command
  could send it anywhere; Kayden's data folder holds every conversation and the settings
  and usage that bound the agent, which it must not change itself. The review prompt
  repeats both rules for calls the patterns miss.
- The answer is `ALLOW` or `ASK` on the first line and a short reason on the second.
  Anything else, a model error or an exhausted budget counts as `ASK`, with a reason
  saying so: the review fails closed.
- Stop cancels a running review; the call is not started.
- The outcome is logged (`permission_review`, and the permission record with
  `by: "review"`), without the reason text; an allowed call emits
  `PermissionResolved(ALLOW_ONCE)`.

### Frontends

Prompts end with "Why it asks: …" when there is a reason (`frontends.common.request_note`).
Settings lists the new level first. Loosening to it is confirmed like any loosening.

Choices and limits are listed in `.agents/decisions.md` (Automatic permissions).

## Alternatives considered

- **Rules on the command text** (an allow list of commands, patterns for dangerous ones).
  Cheap and predictable, but the shell has too many ways to say the same thing, and a list
  cannot tell whether a command is what the user asked for.
- **Reviewing every call, confined ones included.** Doubles the cost of a busy turn for
  calls the sandbox already contains.
- **Giving the reviewer the whole transcript.** It would judge better what the agent is
  doing, but the transcript holds web pages and command output, exactly where injected
  instructions come from.
- **The main model, inline** (asking the agent itself whether its call is safe). The agent
  that wants to run a call is the worst judge of it, and a turn would carry the cost of the
  big model.
- **Leaving secrets and Kayden's data to the review.** One rule set fewer, but "look at my
  SSH setup" plainly covers reading `~/.ssh`, so the review would allow exactly the reads
  that leak most; and an agent that can edit `settings.toml` can loosen its own limits.
- **Allowing on review failure.** Fewer interruptions when the provider has a hiccup, but
  a failure would quietly turn the check off.

## Consequences

- Most calls outside the folder that the user plainly asked for run without a prompt; the
  prompts that remain say why they ask.
- **The reviewer can be wrong both ways.** A wrong "ask" costs a click; a wrong "allow"
  runs something unwanted. The sandbox still contains the confined calls, and the rules
  ask for anything hard to undo.
- **Cost.** One small request per call that reaches outside the folder. With no
  maintenance model set it runs on the main model; the docs suggest setting a cheaper one.
- **Latency.** A reviewed call waits for one request before it starts.
- **The agent can phrase calls to look harmless**, or the user's own words can be stretched
  ("clean up" read as "delete everything"). The rules ask for deletions the user did not
  name.
