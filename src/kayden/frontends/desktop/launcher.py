"""kayden-desktop: the desktop app's entry point (also what the Briefcase bundle runs).

A GUI toolkit's loop must be the one that runs, so this creates the QApplication and runs
the instance on qasync's QEventLoop. It reads config.toml and settings.toml like `kayden`,
always uses the desktop frontend, and roots the Tools at ~/Kayden unless a work folder is
set: a double-clicked app's working directory is meaningless.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from kayden.__main__ import DEFAULT_DATA_DIR, load_config
from kayden.runtime import ModuleLoadError, ModuleSpec, run_instance

DEFAULT_WORK_FOLDER = Path("~/Kayden")
FRONTEND = ModuleSpec("kayden.frontends.desktop:factory")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="kayden-desktop", description=__doc__)
    parser.add_argument("--data-dir", type=Path,
                        default=Path(os.environ.get("KAYDEN_DATA_DIR", DEFAULT_DATA_DIR)),
                        help="instance data directory (default: $KAYDEN_DATA_DIR or ~/.kayden)")
    parser.add_argument("--config", type=Path, help="config file (default: <data dir>/config.toml)")
    args = parser.parse_args(argv)
    # load_config also reads the terminal launcher's flags; the app has none of them.
    for name in ("mode", "activation", "conversation", "model", "maintenance_model",
                 "permissions"):
        setattr(args, name, None)
    return args


def main(argv: Sequence[str] | None = None) -> None:
    import qasync
    from PySide6.QtWidgets import QApplication, QMessageBox

    args = parse_args(argv)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Kayden")
    app.setDesktopFileName("io.github.k5031.kayden")
    app.setQuitOnLastWindowClosed(False)  # the runtime shuts down first, then asyncio.run ends
    root = DEFAULT_WORK_FOLDER.expanduser()
    try:
        config = load_config(args, default_root=root)
    except SystemExit as e:  # a bad config file: the message would go to an unseen stderr
        if isinstance(e.code, str):
            QMessageBox.critical(None, "Kayden", f"Kayden could not start: {e.code}")
        raise
    config = replace(config, frontend=FRONTEND)
    if (config.runtime.tools_module or config.tools).options.get("root") == str(root):
        root.mkdir(parents=True, exist_ok=True)
    try:
        asyncio.run(run_instance(config), loop_factory=qasync.QEventLoop)
    except ModuleLoadError as e:
        QMessageBox.critical(None, "Kayden", f"Kayden could not start: {e}")
        raise SystemExit(1) from None

if __name__ == "__main__":
    main()
