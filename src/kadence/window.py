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

from kadence.__main__ import DEFAULT_DATA_DIR, Launch, run_frontends
from kadence.runtime.modules import WINDOW


def parse_args(argv: Sequence[str] | None = None) -> Launch:
    parser = argparse.ArgumentParser(prog="kadence-desktop", description=__doc__)
    parser.add_argument("--data-dir", type=Path,
                        default=Path(os.environ.get("KADENCE_DATA_DIR", DEFAULT_DATA_DIR)),
                        help="instance data directory (default: $KADENCE_DATA_DIR or ~/.kadence)")
    parser.add_argument("--config", type=Path, help="config file (default: <data dir>/config.toml)")
    parser.add_argument("--conversation", help="open this conversation first")
    return Launch(**vars(parser.parse_args(argv)))


def main(argv: Sequence[str] | None = None) -> None:
    run_frontends(parse_args(argv), WINDOW)


if __name__ == "__main__":
    main()
