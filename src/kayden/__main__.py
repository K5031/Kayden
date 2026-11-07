"""Run Kayden in a terminal: kayden [options] (or python -m kayden).

Builds one instance from an InstanceConfig. A TOML config file (--config, else
<data dir>/config.toml) names the modules and the per-instance runtime settings (model,
maintenance model, permissions, budget); command-line flags override it. Anything it leaves
out is a default: SQLite Memory, the Anthropic Core, shell/file Tools rooted at the current
directory, the Textual TUI and no budget.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kayden.api import Activation, Budget, Mode, PermissionPolicy
from kayden.runtime import (
    InstanceConfig,
    ModuleLoadError,
    ModuleSpec,
    RuntimeConfig,
    run_instance,
)

DEFAULT_DATA_DIR = Path("~/.kayden")
CONFIG_FILE = "config.toml"
DEFAULT_FACTORIES = {
    "core": "kayden.core.anthropic:factory",
    "memory": "kayden.memory.sqlite:factory",
    "tools": "kayden.tools.shell_files:factory",
    "frontend": "kayden.frontends.tui:factory",
}
RUNTIME_KEYS = ("model", "maintenance_model", "permissions", "budget")
BUDGET_INTS = ("max_tokens", "max_tool_calls")
BUDGET_NUMBERS = ("max_cost_usd", "max_duration_s")


def read_config(path: Path, *, required: bool) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        if required:
            raise SystemExit(f"config file {path} not found") from None
        return {}
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"{path}: invalid TOML: {e}") from None
    for name in data:
        if name not in (*DEFAULT_FACTORIES, "runtime"):
            raise SystemExit(f"{path}: unknown section {name!r}; "
                             f"known: {', '.join(DEFAULT_FACTORIES)}, runtime")
    return data


def table(path: Path, data: dict[str, Any], name: str, keys: Sequence[str]) -> dict[str, Any]:
    value = data.get(name.rpartition(".")[2], {})
    if not isinstance(value, dict):
        raise SystemExit(f"{path}: [{name}] must be a table")
    for key in value:
        if key not in keys:
            raise SystemExit(f"{path}: unknown key {key!r} in [{name}]")
    return value


def load_modules(path: Path, data: dict[str, Any]) -> dict[str, ModuleSpec]:
    """One table per module with optional `factory` and `options`. A module the file
    leaves out gets its default factory."""
    specs = {}
    for name, default in DEFAULT_FACTORIES.items():
        section = table(path, data, name, ("factory", "options"))
        factory, options = section.get("factory", default), section.get("options", {})
        if not isinstance(factory, str):
            raise SystemExit(f"{path}: [{name}] factory must be a string")
        if not isinstance(options, dict):
            raise SystemExit(f"{path}: [{name}] options must be a table")
        specs[name] = ModuleSpec(factory, options)
    return specs


def load_runtime(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    """RuntimeConfig fields from [runtime]: model, maintenance_model, permissions, budget."""
    section = table(path, data, "runtime", RUNTIME_KEYS)
    settings: dict[str, Any] = {}
    for key in ("model", "maintenance_model"):
        if key in section:
            if not isinstance(section[key], str):
                raise SystemExit(f"{path}: [runtime] {key} must be a string")
            settings[key] = section[key]
    if "permissions" in section:
        try:
            settings["permissions"] = PermissionPolicy(section["permissions"])
        except ValueError:
            raise SystemExit(f"{path}: [runtime] permissions must be one of "
                             f"{', '.join(PermissionPolicy)}") from None
    budget = table(path, section, "runtime.budget", (*BUDGET_INTS, *BUDGET_NUMBERS))
    for key, value in budget.items():
        integral = key in BUDGET_INTS
        kinds = (int,) if integral else (int, float)
        if isinstance(value, bool) or not isinstance(value, kinds) or value < 0:
            kind = "integer" if integral else "number"
            raise SystemExit(f"{path}: [runtime.budget] {key} must be a non-negative {kind}")
    if budget:
        settings["budget"] = Budget(**budget)
    return settings


def build_config(argv: Sequence[str] | None = None) -> InstanceConfig:
    parser = argparse.ArgumentParser(prog="kayden", description=__doc__)
    parser.add_argument("--data-dir", type=Path,
                        default=Path(os.environ.get("KAYDEN_DATA_DIR", DEFAULT_DATA_DIR)),
                        help="instance data directory (default: $KAYDEN_DATA_DIR or ~/.kayden)")
    parser.add_argument("--config", type=Path,
                        help=f"config file (default: <data dir>/{CONFIG_FILE})")
    parser.add_argument("--mode", choices=[m.value for m in Mode],
                        help="start right away in this mode (default: ask)")
    parser.add_argument("--activation", choices=[a.value for a in Activation],
                        default=Activation.USER_CONTROLLED.value,
                        help="Continuous Mode activation policy")
    parser.add_argument("--conversation", help="Session conversation id to resume")
    parser.add_argument("--model", help="model id (default: the first the Core lists)")
    parser.add_argument("--maintenance-model", help="cheaper model for memory maintenance")
    parser.add_argument("--permissions", choices=[p.value for p in PermissionPolicy],
                        help="tool permission policy (default: ask)")
    args = parser.parse_args(argv)

    data_dir = args.data_dir.expanduser()
    path = args.config.expanduser() if args.config else data_dir / CONFIG_FILE
    data = read_config(path, required=args.config is not None)
    modules = load_modules(path, data)
    runtime = load_runtime(path, data)
    for key in ("model", "maintenance_model"):
        if getattr(args, key):
            runtime[key] = getattr(args, key)
    if args.permissions:
        runtime["permissions"] = PermissionPolicy(args.permissions)
    if modules["tools"].factory == DEFAULT_FACTORIES["tools"] and not modules["tools"].options:
        modules["tools"] = ModuleSpec(modules["tools"].factory, {"root": str(Path.cwd())})
    if args.mode:
        start = {"mode": args.mode, "activation": args.activation}
        if args.conversation:
            start["conversation_id"] = args.conversation
        frontend = modules["frontend"]
        modules["frontend"] = ModuleSpec(frontend.factory, {**frontend.options, **start})
    return InstanceConfig(
        runtime=RuntimeConfig(data_dir=data_dir, **runtime),
        **modules,
    )


def main(argv: Sequence[str] | None = None) -> None:
    try:
        asyncio.run(run_instance(build_config(argv)))
    except ModuleLoadError as e:
        print(f"kayden: {e}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
