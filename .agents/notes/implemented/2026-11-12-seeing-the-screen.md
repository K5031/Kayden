# Agent Note: Seeing the screen

Status: implemented — a `screenshot` tool in the built-in Tools module lets the agent look at the user's desktop

## Problem

Kayden works on the user's computer but is blind to it: it reads files and command
output, not what is on screen. The owner wants to say "look at my screen" or "what is
wrong with this window", typed or in voice mode, and have the agent see it.

## Decision

A tool `screenshot`, without arguments, in `kayden.tools.shell_files` (`screen.py`). It
captures the whole desktop, all monitors, and returns the picture as an attachment of the
tool result, the way `read_file` returns a picture, so every Core that shows pictures to
its model shows this one. Both apps have it, since it belongs to the Tools module.

- **Capture** is done by KDE's Spectacle (`spectacle -b -n -f -o <file>`), run as a child
  process: on Wayland a program cannot read the screen itself, and Spectacle is the one
  KDE lets do it without a prompt or a notification. The tool is offered only where
  `spectacle` is found. The picture is written to a temporary folder that is removed
  after it is read.
- **Permission** is the runtime's, unchanged: the tool is neither read-only nor confined
  to the work folder, so it asks under every level but Auto, and under Auto the reviewer
  lets it run only when the user's own messages ask for a look. Its description tells
  the agent not to take a picture the user did not ask for.
- **Seeing only.** The tool does not click or type.

## Alternatives considered

- **A button that attaches a screenshot to the user's message.** The agent then cannot
  look by itself during a task or a voice conversation, which is the point.
- **Continuous screen sharing.** Every look costs tokens and sends the screen to the model
  provider; a look on request is enough.
- **The desktop portal (xdg-desktop-portal Screenshot).** Works on every Wayland desktop,
  but asks the user in a dialog of its own on each capture, on top of Kayden's permission,
  and needs a D-Bus library.
- **Capturing in the desktop frontend with Qt.** Qt cannot read the screen on Wayland
  either, and the terminal app would have no tool.
- **An `active window` target.** While the user types or talks to Kayden, the active
  window is Kayden's own.
- **A module of its own.** One tool; the built-in module already carries the web tools
  beside shell and files.

## Consequences

- Checked on the owner's desktop (KDE Plasma on Wayland, Spectacle 6.8): a call returned
  a 1920 x 1080 PNG of 174 kB without a prompt or a notification. Not checked with a real
  model.
- Everything on screen goes to the model provider: other chats, a password in view.
- A capture stays in the conversation, at full size in the archive, and is paid for again
  with every later request.
- KDE only; on other desktops the tool is not offered.
- A capture over 5 MB (several large monitors) is refused, as providers refuse it; the
  picture is not scaled down.
- Providers scale a large screen down, so small text can blur.
