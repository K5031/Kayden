"""Which launcher the bundle's program runs, by the name it was started as."""

from __future__ import annotations

from collections.abc import Callable

TERMINAL_NAMES = ("kayden", "kd")  # the commands the package links to the program


def launcher_for(name: str) -> Callable[[], None]:
    if name in TERMINAL_NAMES:
        from kayden.__main__ import main
    else:
        from kayden.window import main
    return main
