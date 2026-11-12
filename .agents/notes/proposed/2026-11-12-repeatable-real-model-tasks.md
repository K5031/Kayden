# Agent Note: Repeatable real-model tasks

Status: proposed — a small set of tasks run with a real model before and after a change to what the agent is told, so that "better" is measured

## Problem

Kayden's tests never use a real model: they script what the model says. They show that
the runtime does the right thing with a given answer, and nothing about what a model does
with Kayden's prompt, tool descriptions and notices.

That second question has been answered by running one agent once and reading what it did.
On 2026-11-12 alone:

- A rule added to `finish_goal`'s description was judged by one run; it failed, was
  reworded, and was judged by one more.
- `kfm resume` passed every test and did nothing with a real agent: the tests had always
  sent the message a real run did not.
- Wording was added to tool descriptions and briefs on a hunch several times, which is how
  a prompt becomes a pile of patches for last week's runs.

Three proposals now wait on the same missing thing: the base page of working guidance
([note](2026-11-12-working-guidance-in-the-prompt.md)), any change to a fleet's briefs
(KFM), and deciding whether a fleet needs a memory its agents share (KFM).

## Proposal

A folder of tasks outside the tests, never run by `uv run pytest`, and a command that
runs them with the owner's own configuration.

- **A task** is a scratch folder to start from, an instruction, a spending limit, and a
  check written as code: the files that must exist, the tests that must pass, what must
  not have been touched. It runs headless, the way a fleet's unit does.
- **What is recorded** for each run: passed or not, requests, cost, time, how often the
  agent was sent back, and the commit and model it ran on; appended to a file that is
  not in the repository.
- **Each task runs several times**, since one run of a model proves little; a change is
  compared on how often a task passes and what it costs, before and after.
- **Few and broad**: about eight tasks, each standing for a habit rather than a case.
  Fix a fault that tests reveal. Do a task of several steps and keep the plan true. Reach
  a goal and report without being sent back. Ask instead of guessing when the instruction
  cannot be carried out as written. Stay inside the files the task names. Answer a spoken
  question in a way that can be listened to.
- **The owner starts a run**: it spends real money, and nothing runs it automatically.
- KFM takes the same shape for fleets: a role or a task for a fleet, a check, the cost.

## Alternatives considered

- **One run, read by hand**, as now. Cheap, and it found real faults. It cannot tell a
  change that helped from a lucky run, and nobody repeats last month's runs after this
  month's change.
- **More tests with a scripted model.** They cannot fail the way a model fails: the
  script says what the test's author expected a model to say.

## Acceptance criteria

- One command runs every task and prints, per task, how often it passed and what it cost.
- Run twice on the same commit and model, the results are close enough that a real
  change shows.
- The base page of working guidance is accepted or rejected on these results.

## Risks

- **Money.** About eight tasks, three runs each, on a mid-priced model: a few dollars a
  pass; more on the top model.
- **The tasks become the target.** Wording tuned until eight tasks pass fits eight tasks.
  They have to stay broad, and be changed when they stop telling anything.
- **A check sees the outcome, not the judgment.** Whether an agent asked at the right
  moment or changed what nobody asked for is hard to check in code; some of it will still
  be read by hand.
- Results move with the provider's model, not only with Kayden's changes.
- Upkeep: a task that no longer fits Kayden's tools fails for the wrong reason.
