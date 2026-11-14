# Agent Note: A floating bar

Status: rejected — a small always-reachable input strip for Kadence was considered and dropped: it buys one thing, and that thing is cheaper another way

## Problem

Asking Kadence something means switching to its window. A floating bar, summoned over
whatever is on screen (as Spotlight or KRunner are), would take a question without that
switch.

## Decision

Not built. If the wish for a quick ask comes back, the answer is a keyboard shortcut that
brings Kadence's window forward on a new chat with the cursor in the message box: the
hand-over to a running instance exists (`single.py`), so it is a command to bind to a key
in the desktop's settings, and nothing new to keep in step.

## Alternatives considered

- **A second window on the same app**, summoned by a hotkey: an input, the voice button,
  the current step, the reply, a permission card. It would be a second interface to keep
  in step with every change to steps, cards, the checklist and voice. Kadence's tasks are
  not one-liners: they run commands, ask permission and work through a plan, so most
  conversations in a bar would end with the full window being opened.
- **A bar that stays visible** while the user works in other windows. On Wayland a window
  cannot pin itself above others or choose its position; it needs a KWin window rule, or
  KDE's layer-shell library, which PySide does not expose.

## Consequences

- No hotkey in Kadence itself: on Wayland an application cannot take a global one, so the
  key is bound in the desktop's settings.
- Voice already covers use without reaching for the window.
- The work goes to the direction in `PHILOSOPHY.md`: an agent that works unsupervised,
  not polish on the supervised side.
