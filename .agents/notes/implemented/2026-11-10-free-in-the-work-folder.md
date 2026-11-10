# Agent Note: Free in the work folder

Status: implemented — by default the agent works without asking inside its work folder, which file tools check by path and the shell enforces with a sandbox

## Problem

An agent that asks before each file it writes and each command it runs is tiring to use,
and a task cannot run while the user is away. The natural rule is "do what you like in
your own folder, ask before touching anything else".

The work folder was not a boundary, so that rule could not be offered:

- The file tools accepted any path. A relative path landed in the work folder, an
  absolute one (`~/.ssh/id_rsa`) was read or written as given.
- The shell tool started in the work folder, and its commands reached everything the
  user's account could.

Not asking "inside the work folder" would have meant not asking at all.

## Decision

### A permission level: free in the work folder

`PermissionPolicy.ALLOW_WORKSPACE`, shown as "Work freely in the work folder, ask before
anything else". A tool call that is **confined** runs without asking; every other call
prompts, read-only ones included. The default, `auto`, builds on it: confined calls run the
same way, and the others are reviewed first
([automatic permissions](2026-11-10-auto-permissions.md)).

| Level | Runs without asking |
|---|---|
| `deny` | nothing |
| `ask` | nothing |
| `allow_read_only` | read-only tools, on any path |
| `allow_workspace` | confined calls |
| `auto` (default) | confined calls, and the calls a review allows |

That is also the strictness order used for an inherited ceiling. Loosening to it in a
running instance is confirmed like any loosening.

### Tools say whether a call is confined

Only the Tools module knows its work folder and what a call touches, and the runtime owns
the decision. `Tools.confined(call) -> bool` is optional: the base class returns False, so
a Tools module that does not implement it behaves under `allow_workspace` as under `ask`.
The runtime calls it before the permission decision; an exception counts as False.
`PermissionRequest.confined` carries the answer to the frontends, whose prompts say whether
the call stays inside the work folder or can reach outside it.

### File tools: by path

A file call is confined when its path, with symlinks followed, is inside the work folder
(`ShellFilesTools._inside`). The tools' behavior is unchanged: a path outside still works,
after the user allows it.

### Shell: a sandbox

The shell tool takes an argument, `unsandboxed` (default false).

- **Sandboxed** when the sandbox works: the command runs under
  [bubblewrap](https://github.com/containers/bubblewrap) and the call is confined. The
  tool's description tells the model what the sandbox hides.
- **`unsandboxed: true`**, or no working sandbox: the command runs with the user's rights
  and is not confined, so it prompts.

Inside the sandbox (`shell_files._sandbox`):

| | |
|---|---|
| Work folder | read and write |
| System (`/usr`, `/etc`, ...) | read-only, so installed programs run |
| `/home`, `/root`, the user's home | empty, except the work folder itself |
| `/tmp` | private and empty |
| `/run/user` | hidden, so the session bus and its services (the keychain) cannot be reached |
| Environment | as outside, minus variables whose names look like secrets (`KEY`, `TOKEN`, `SECRET`, `PASSW`, `CREDENTIAL`) |
| Processes | only its own (`--unshare-pid`) |
| Network | as outside |

```
bwrap --ro-bind / / --tmpfs /home --tmpfs /root --tmpfs /run/user --tmpfs /tmp
      --dir HOME --bind ROOT ROOT --dev /dev --proc /proc --unshare-pid
      --die-with-parent --chdir ROOT sh -c COMMAND
```

Whether it works is tested once when the module is built, by running `true` in it, off
the event loop: `bwrap` missing, user namespaces disabled, or Kayden itself running in a
sandbox all leave the shell unsandboxed. Stop, cancellation and timeouts act on the
`bwrap` process group as they did on the shell's.

### A work folder that cannot be a boundary

When the work folder is `/`, the user's home folder, or contains it, nothing is confined
and every call prompts. The desktop app's default folder, `~/Kayden`, qualifies; a terminal
start in `~` does not.

## Alternatives considered

- **Not asking inside the work folder, without a sandbox.** One line of policy, but shell
  commands cannot be told apart by where they act, so every command would run unasked
  with the user's full rights.
- **Deciding from the command text** (allow `ls`, `cat`, a list of safe commands). Brittle:
  shells have too many ways to reach a file, and a wrong guess runs unasked.
- **Read-only home instead of a hidden one.** Tools installed under the home folder
  (`~/.cargo`, `~/.local/bin`, caches) would keep working, but with the network allowed a
  command could read keys and tokens there and send them anywhere. `unsandboxed` covers
  the commands that need more.
- **No network in the sandbox.** The strongest containment, but it breaks installing
  packages and fetching anything, which agents do constantly.
- **Landlock, or a container (Docker, Podman).** Landlock needs no helper binary but a
  recent kernel and code of our own to drive it; containers need a daemon or an image and
  change what programs are installed. bubblewrap is one small established binary that
  reuses the host system.
- **Making the file tools refuse paths outside the work folder.** Simpler than asking, but
  the user may well want a file elsewhere read or written, with their consent.
- **`confined` as a flag on `ToolSpec`.** A spec is per tool, but whether a file call is
  confined depends on its path, so it is decided per call.
- **A line in Settings when the work folder is too broad or the sandbox is missing.** It
  needs a way for a Tools module to tell frontends about itself. The prompts already say
  that a call can reach outside the work folder; the docs explain why.

## Consequences

- **Hands-off work by default.** In a folder made for it, the agent reads, writes and runs
  commands without a prompt, and still asks before anything outside.
- **A sandbox bug is a security bug.** A mistake in the bubblewrap arguments lets an
  unasked command out. A test probes the boundary from inside: writing outside, listing
  the home folder, reaching the session bus.
- **Secrets in the environment stay out.** Keys exported in a shell profile reach Kayden
  started from a terminal; a sandboxed command runs unasked and may use the network, so it
  gets the environment without them (`shell_files.sandbox_environment`). A secret under
  another name still gets through; unsandboxed commands, which are checked first, keep
  the whole environment.
- **Network plus the work folder.** A confined command can send anything in the work
  folder anywhere. The docs say to keep secrets out of it.
- **Tools under the home folder do not work in the sandbox** (toolchains, caches, `uv`,
  `cargo`). The agent asks to run such commands unsandboxed, which brings a prompt back
  for them. A list of extra read-only paths is the next step if that proves common.
- **Conversation grants follow the boundary.** "Allow for this conversation" given for a
  confined call covers that tool's confined calls only. Given for a call that can reach
  outside, such as an unsandboxed command, it covers every later call of that tool in the
  conversation; the prompt says the call can reach outside.
- **Reads and the ceiling.** `allow_read_only` reads anywhere without asking and
  `allow_workspace` does not, so the two are not strictly ordered; the ceiling orders them
  by what may be changed.
- **Linux only.** Without bubblewrap (macOS, some Linux setups, a Kayden that itself runs
  in a sandbox) shell commands ask; file calls in the work folder still run freely.
- **A looser default.** New users get an agent that changes files in its folder unasked.
  The folder is theirs to choose, the desktop app defaults to `~/Kayden`, and the welcome
  says what the agent may do.
