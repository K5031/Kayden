# Agent Note: Free in the work folder

Status: proposed — by default the agent works without asking inside its work folder, which file tools check by path and the shell enforces with a sandbox

## Problem

By default Kayden asks before every tool call. An agent that asks before each file it
writes and each command it runs is tiring to use, and a task cannot run while the user is
away. The other level that allows anything, "Read freely, ask before changes", still asks
before every change.

The natural rule is "do what you like in your own folder, ask before touching anything
else". Kayden cannot offer it today, because the work folder is not a boundary:

- The file tools accept any path. A relative path lands in the work folder, an absolute
  one (`~/.ssh/id_rsa`) is read or written as given.
- The shell tool starts in the work folder, and its commands reach everything the user's
  account can.

Not asking "inside the work folder" would therefore mean not asking at all.

## Proposal

### A permission level: free in the work folder

`PermissionPolicy.ALLOW_WORKSPACE`, the new default, shown as "Work freely in the work
folder, ask before anything outside it". A tool call that is **confined** runs without
asking; every other call prompts, as under ASK.

| Level | Runs without asking |
|---|---|
| `deny` | nothing |
| `ask` | nothing |
| `allow_read_only` | read-only tools, on any path |
| `allow_workspace` (default) | confined calls |

Strictness order, used when a ceiling is inherited: `deny`, `ask`, `allow_read_only`,
`allow_workspace`. Loosening to it in a running instance is confirmed like any loosening.

### Tools say whether a call is confined

Only the Tools module knows its work folder and what a call touches, and the runtime owns
the decision. `Tools` gains one optional method:

```python
async def confined(self, call: ToolCall) -> bool:
    """True when this call can read and change nothing outside the module's work
    folder. The runtime asks before running a call that is not."""
```

The base class returns False, so a Tools module that does not implement it behaves under
`allow_workspace` exactly as under `ask`. The runtime calls it only under that level, once
per call, before the permission decision; built-in tools stay ungated.

### File tools: by path

A file call is confined when its path, with symlinks resolved, is inside the work folder.
A symlink that leads out of the folder is outside it. The tools' behavior is unchanged:
a path outside still works, after the user allows it.

### Shell: a sandbox

The shell tool takes a new argument, `unsandboxed` (default false).

- **Sandboxed (the default)** when the sandbox is available: the command runs under
  [bubblewrap](https://github.com/containers/bubblewrap), the sandbox Flatpak uses. The
  call is confined.
- **`unsandboxed: true`**, or no sandbox available: the command runs as today and is not
  confined, so it prompts. The model is told to ask for this only when a command needs
  something the sandbox hides, and to say why.

Inside the sandbox:

| | |
|---|---|
| Work folder | read and write |
| System (`/usr`, `/etc`, ...) | read-only, so installed programs run |
| Home folders, `/root` | hidden (empty), except the work folder itself |
| `/tmp` | private and empty |
| `/run/user` | hidden, so the session bus and its services cannot be reached |
| Processes | only its own (`--unshare-pid`) |
| Network | allowed |

The command line, verified on Arch with bubblewrap 0.13.1:

```
bwrap --ro-bind / / --tmpfs /home --tmpfs /root --tmpfs /run/user --tmpfs /tmp
      --bind ROOT ROOT --dev /dev --proc /proc --unshare-pid --die-with-parent
      --chdir ROOT sh -c COMMAND
```

Availability is tested once when the module is built, by running `true` in the sandbox
off the event loop: `bwrap` missing, user namespaces disabled, or Kayden itself running
inside a sandbox all make it unavailable. Stop, cancellation and timeouts act on the
`bwrap` process group as they do on the shell's today; `--die-with-parent` ends the
command with it.

### A work folder that cannot be a boundary

When the work folder is `/`, the user's home folder, or contains it, nothing is confined:
every call prompts, and Settings says the work folder is too broad to work in freely.
The desktop app's default folder, `~/Kayden`, qualifies; a terminal start in `~` does not.

### Frontends and docs

- Settings lists the new level first among the allowing ones; the TUI and the desktop app
  share the label.
- A permission prompt for a shell call with `unsandboxed` says it runs outside the sandbox.
- `docs/settings.md` (Permissions), `docs/getting-started.md` and `docs/concepts.md` say
  what the level allows, what the sandbox hides, and that without bubblewrap (macOS, some
  Linux setups) shell commands ask as before.

## Alternatives considered

- **Not asking inside the work folder, without a sandbox.** One line of policy, but shell
  commands cannot be told apart by where they act, so every command would run unasked
  with the user's full rights.
- **Deciding from the command text** (allow `ls`, `cat`, a list of safe commands). Brittle:
  shells have too many ways to reach a file, and a wrong guess runs unasked.
- **Read-only home instead of a hidden one.** Tools installed under the home folder
  (`~/.cargo`, `~/.local/bin`, caches) would keep working, but with the network allowed a
  command could read keys and tokens there and send them anywhere. Hidden is the safe
  default; `unsandboxed` covers the commands that need more.
- **No network in the sandbox.** The strongest containment, but it breaks installing
  packages and fetching anything, which agents do constantly.
- **Landlock, or a container (Docker, Podman).** Landlock needs no helper binary but a
  recent kernel and code of our own to drive it; containers need a daemon or an image and
  change what programs are installed. bubblewrap is one small established binary that
  reuses the host system.
- **Making the file tools refuse paths outside the work folder.** Simpler than asking, but
  the user may well want a file elsewhere read or written, with their consent.
- **`confined` as a flag on `ToolSpec`.** A spec is per tool, but whether a file call is
  confined depends on its path, so it has to be decided per call.

## Acceptance criteria

- Tests, written first:
  - under `allow_workspace`, a confined call runs without a prompt and an unconfined one
    prompts; a Tools module without `confined` prompts for everything
  - file tools: a path inside the work folder is confined; an absolute path outside, a
    `..` path leading out and a symlink leading out are not
  - shell, with bubblewrap present: a sandboxed command can write in the work folder and
    cannot write outside it, cannot list the home folder, and cannot reach the session
    bus; `unsandboxed: true` is not confined
  - shell, with bubblewrap absent or failing its check: nothing is confined, and commands
    run as today
  - a work folder of `~` or `/` confines nothing
  - Stop and cancellation end a sandboxed command and its children
  - the ceiling clamps in the stated order
- The Tools conformance suite covers `confined`; the fake Tools implements it.
- SPEC (10, permissions), `api.py`, both frontends and `docs/` change in the same change.
- The default changes only for instances with no saved or configured permission level.

## Risks

- **A sandbox bug is a security bug.** A mistake in the bubblewrap arguments lets an
  unasked command out. The acceptance tests probe the boundary from inside, and the
  command line stays short enough to read.
- **Network plus the work folder.** A confined command can send anything in the work
  folder anywhere. The work folder is what the user gave the agent; docs say not to keep
  secrets in it.
- **Tools under the home folder stop working in the sandbox** (toolchains, caches, `uv`,
  `cargo`). The agent then asks to run unsandboxed, which brings prompts back for those
  commands. If that is common in practice, a list of extra read-only paths is the next
  step; it is left out until it is needed.
- **Reads and the ceiling.** `allow_read_only` reads anywhere without asking and
  `allow_workspace` does not, so the two are not strictly ordered; the ceiling orders them
  by what may be changed.
- **Linux only for now.** On macOS the shell keeps asking; `sandbox-exec` could confine it
  later. A packaged Kayden that itself runs in a sandbox (Flatpak) cannot nest bubblewrap
  and falls back to asking.
- **A looser default.** New users get an agent that changes files in its folder unasked.
  The folder is theirs to choose, the desktop app defaults to an empty `~/Kayden`, and
  the welcome says what the agent may do.
