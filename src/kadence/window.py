"""The window launcher, which `kadence` hands over to and the desktop bundle runs.

It reads config.toml and settings.toml like `kadence`, runs the saved frontend when it is a
window frontend (else the desktop app), each on the event loop its factory names, and
roots the Tools at ~/Kadence unless a work folder is set: a double-clicked app's working
directory is meaningless. Restart now starts the instance again in this process, or on
another loop when the next frontend needs one. Nothing here assumes a GUI toolkit.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from kadence.__main__ import (
    DEFAULT_DATA_DIR,
    LAUNCHER_FRONTENDS,
    frontend_loop,
    load_config,
    run_frontends,
)
from kadence.runtime import ModuleSpec
from kadence.runtime.modules import WINDOW, targets

DEFAULT_WORK_FOLDER = Path("~/Kadence")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="kadence-desktop", description=__doc__)
    parser.add_argument("--data-dir", type=Path,
                        default=Path(os.environ.get("KADENCE_DATA_DIR", DEFAULT_DATA_DIR)),
                        help="instance data directory (default: $KADENCE_DATA_DIR or ~/.kadence)")
    parser.add_argument("--config", type=Path, help="config file (default: <data dir>/config.toml)")
    parser.add_argument("--conversation", help="open this conversation first")
    args = parser.parse_args(argv)
    # load_config also reads the terminal launcher's flags; the window launcher has none.
    for name in ("model", "maintenance_model", "permissions", "duty", "goal"):
        setattr(args, name, None)
    args.headless = False
    return args


async def _fail(error: SystemExit) -> None:
    raise error


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    root = DEFAULT_WORK_FOLDER.expanduser()
    try:
        config = load_config(args, default_root=root, launcher=WINDOW)
    except SystemExit as e:
        # A bad config file, before any frontend is chosen: the message would go to an
        # unseen stderr, so the launcher's own frontend shows it on its loop.
        own = ModuleSpec(targets("frontend")[LAUNCHER_FRONTENDS[WINDOW]])
        frontend_loop(own)(_fail(e))
        raise
    if config.tools.options.get("root") == str(root):
        root.mkdir(parents=True, exist_ok=True)
    run_frontends(args, WINDOW, default_root=root)


if __name__ == "__main__":
    main()
