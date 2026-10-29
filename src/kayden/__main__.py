"""Run Kayden in a terminal: python -m kayden [options].

Builds one instance from an InstanceConfig: SQLite Memory, the Anthropic Core,
shell/file Tools rooted at the current directory and the Textual TUI.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from kayden.api import Activation, Mode, PermissionPolicy
from kayden.runtime import InstanceConfig, ModuleSpec, RuntimeConfig, run_instance

DEFAULT_DATA_DIR = Path("~/.kayden")


def build_config(argv: Sequence[str] | None = None) -> InstanceConfig:
    parser = argparse.ArgumentParser(prog="python -m kayden", description=__doc__)
    parser.add_argument("--data-dir", type=Path,
                        default=Path(os.environ.get("KAYDEN_DATA_DIR", DEFAULT_DATA_DIR)),
                        help="instance data directory (default: $KAYDEN_DATA_DIR or ~/.kayden)")
    parser.add_argument("--mode", choices=[m.value for m in Mode],
                        help="start right away in this mode (default: ask)")
    parser.add_argument("--activation", choices=[a.value for a in Activation],
                        default=Activation.USER_CONTROLLED.value,
                        help="Continuous Mode activation policy")
    parser.add_argument("--conversation", help="Session conversation id to resume")
    parser.add_argument("--model", help="Anthropic model id (default: the first listed)")
    parser.add_argument("--maintenance-model", help="cheaper model for memory maintenance")
    parser.add_argument("--permissions", choices=[p.value for p in PermissionPolicy],
                        default=PermissionPolicy.ASK.value)
    args = parser.parse_args(argv)

    start: dict[str, str] = {}
    if args.mode:
        start = {"mode": args.mode, "activation": args.activation}
        if args.conversation:
            start["conversation_id"] = args.conversation
    return InstanceConfig(
        runtime=RuntimeConfig(
            data_dir=args.data_dir.expanduser(),
            permissions=PermissionPolicy(args.permissions),
            model=args.model,
            maintenance_model=args.maintenance_model,
        ),
        core=ModuleSpec("kayden.core.anthropic:factory"),
        memory=ModuleSpec("kayden.memory_sqlite:factory"),
        tools=ModuleSpec("kayden.tools.shell_files:factory", {"root": str(Path.cwd())}),
        frontend=ModuleSpec("kayden.frontends.tui:factory", start),
    )


def main(argv: Sequence[str] | None = None) -> None:
    config = build_config(argv)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set.")
    asyncio.run(run_instance(config))


if __name__ == "__main__":
    main()
