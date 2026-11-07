"""Run Kayden in a terminal: kayden [options] (or python -m kayden).

Builds one instance from an InstanceConfig. A TOML config file (--config, else
<data dir>/config.toml) names the modules and the per-instance runtime settings (model,
maintenance model, permissions, budget, system prompt); command-line flags override it.
Anything it leaves out is a default: SQLite Memory, the Anthropic Core, shell/file Tools
rooted at the current directory, the Textual TUI and no budget. --show-config prints the
result.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

import tomli_w

from kayden.api import Activation, Budget, Mode, PermissionPolicy
from kayden.runtime import (
    InstanceConfig,
    ModuleLoadError,
    ModuleSpec,
    Provider,
    RuntimeConfig,
    run_instance,
)
from kayden.runtime.settings import SETTINGS_FILE

DEFAULT_DATA_DIR = Path("~/.kayden")
CONFIG_FILE = "config.toml"
OPENAI_COMPAT = "kayden.core.openai_compat:factory"
DEFAULT_FACTORIES = {
    "core": "kayden.core.anthropic:factory",
    "memory": "kayden.memory.sqlite:factory",
    "tools": "kayden.tools.shell_files:factory",
    "frontend": "kayden.frontends.tui:factory",
}
RUNTIME_KEYS = ("model", "maintenance_model", "permissions", "budget", "system_prompt",
                "system_prompt_file")
SECRET_WORDS = ("key", "token", "secret", "password")
PROVIDERS = (
    Provider("anthropic", "Claude", ModuleSpec(DEFAULT_FACTORIES["core"]), "ANTHROPIC_API_KEY"),
    Provider("deepseek", "DeepSeek", ModuleSpec(OPENAI_COMPAT, {"profile": "deepseek"}),
             "DEEPSEEK_API_KEY"),
    Provider("openai", "OpenAI", ModuleSpec(OPENAI_COMPAT, {"profile": "openai"}),
             "OPENAI_API_KEY"),
)
SAVED_KEYS = ("provider", "model", "maintenance_model", "permissions", "system_prompt",
              "work_folder", "budget")
BUDGET_INTS = ("max_tokens", "max_tool_calls")
BUDGET_NUMBERS = ("max_cost_usd", "max_duration_s")


def read_config(path: Path, *, required: bool,
                sections: Sequence[str] | None = None) -> dict[str, Any]:
    """The TOML file's top level; with `sections`, those are the only keys allowed."""
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        if required:
            raise SystemExit(f"config file {path} not found") from None
        return {}
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"{path}: invalid TOML: {e}") from None
    for name in data:
        if sections is not None:
            if name not in sections:
                raise SystemExit(f"{path}: unknown key {name!r}; known: {', '.join(sections)}")
        elif name not in (*DEFAULT_FACTORIES, "runtime"):
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
    """RuntimeConfig fields from [runtime]: model, maintenance_model, permissions, budget,
    and system_prompt (inline, or system_prompt_file relative to the config file)."""
    section = table(path, data, "runtime", RUNTIME_KEYS)
    settings: dict[str, Any] = {}
    for key in ("model", "maintenance_model", "system_prompt", "system_prompt_file"):
        if key in section and not isinstance(section[key], str):
            raise SystemExit(f"{path}: [runtime] {key} must be a string")
    for key in ("model", "maintenance_model"):
        if key in section:
            settings[key] = section[key]
    if "system_prompt" in section and "system_prompt_file" in section:
        raise SystemExit(f"{path}: [runtime] set system_prompt or system_prompt_file, not both")
    if "system_prompt_file" in section:
        prompt_path = path.parent / Path(section["system_prompt_file"]).expanduser()
        try:
            settings["system_prompt"] = prompt_path.read_text()
        except FileNotFoundError:
            raise SystemExit(f"{path}: [runtime] system_prompt_file {prompt_path}: not found"
                             ) from None
    elif "system_prompt" in section:
        settings["system_prompt"] = section["system_prompt"]
    if "system_prompt" in settings and not settings["system_prompt"].strip():
        raise SystemExit(f"{path}: [runtime] system_prompt must not be empty")
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


def redact(options: Any) -> Any:
    """Options with string values under secret-sounding keys replaced."""
    if not isinstance(options, dict):
        return options
    return {k: "<redacted>" if isinstance(v, str) and any(w in k.lower() for w in SECRET_WORDS)
            else redact(v) for k, v in options.items()}


def show_config(config: InstanceConfig, path: Path) -> str:
    """The effective config as TOML that loads back to the same instance."""
    found = "" if path.is_file() else " (not found, defaults only)"
    header = ["# Effective Kayden config", f"# data dir: {config.runtime.data_dir}",
              f"# config file: {path}{found}"]
    if (saved := config.runtime.data_dir / SETTINGS_FILE).is_file():
        header.append(f"# saved settings applied on top: {saved}")
    data: dict[str, Any] = {
        name: {"factory": spec.factory, "options": redact(dict(spec.options))}
        for name, spec in (("core", config.core), ("memory", config.memory),
                           ("tools", config.tools), ("frontend", config.frontend))
    }
    runtime = config.runtime
    section: dict[str, Any] = {}
    for key, unset in (("model", "the first model the Core lists"),
                       ("maintenance_model", "the main model")):
        if (value := getattr(runtime, key)) is None:
            header.append(f"# {key}: not set, {unset}")
        else:
            section[key] = value
    section["permissions"] = runtime.permissions.value
    section["system_prompt"] = runtime.system_prompt
    if budget := {k: v for k, v in asdict(runtime.budget).items() if v is not None}:
        section["budget"] = budget
    data["runtime"] = section
    return "\n".join(header) + "\n\n" + tomli_w.dumps(data, multiline_strings=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
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
    parser.add_argument("--show-config", action="store_true",
                        help="print the effective config as TOML and exit")
    return parser.parse_args(argv)


def config_path(args: argparse.Namespace) -> Path:
    if args.config:
        return args.config.expanduser()
    return args.data_dir.expanduser() / CONFIG_FILE


def build_config(argv: Sequence[str] | None = None) -> InstanceConfig:
    return load_config(parse_args(argv))


def _identity(core: ModuleSpec) -> tuple[str, Any]:
    profile = core.options.get("profile", "openai") if core.factory == OPENAI_COMPAT else None
    return core.factory, profile


def providers_for(core: ModuleSpec) -> tuple[list[Provider], str | None]:
    """The built-in providers, the configured Core's entry carrying its configured options
    (so switching back keeps them), and the id of the configured one (None if custom)."""
    providers, current = [], None
    for provider in PROVIDERS:
        if _identity(provider.core) == _identity(core):
            provider, current = Provider(provider.id, provider.name, core,
                                         provider.key_name), provider.id
        providers.append(provider)
    return providers, current


def read_saved(path: Path, providers: Sequence[Provider]) -> dict[str, Any]:
    """Settings saved by RuntimeAPI.update_settings, validated like [runtime]."""
    data = read_config(path, required=False, sections=SAVED_KEYS)
    for key in ("provider", "work_folder"):
        if key in data and not isinstance(data[key], str):
            raise SystemExit(f"{path}: {key} must be a string")
    if "provider" in data and data["provider"] not in {p.id for p in providers}:
        raise SystemExit(f"{path}: unknown provider {data['provider']!r}")
    runtime = {k: v for k, v in data.items() if k not in ("provider", "work_folder")}
    clear_light = runtime.get("maintenance_model") == ""
    if clear_light:
        del runtime["maintenance_model"]
    saved = load_runtime(path, {"runtime": runtime})
    if clear_light:
        saved["maintenance_model"] = None
    return {**saved, **{k: data[k] for k in ("provider", "work_folder") if k in data}}


def load_config(args: argparse.Namespace) -> InstanceConfig:
    """config.toml, then the saved settings, then command-line flags, each overriding."""
    data_dir = args.data_dir.expanduser()
    path = config_path(args)
    data = read_config(path, required=args.config is not None)
    modules = load_modules(path, data)
    runtime = load_runtime(path, data)
    if modules["tools"].factory == DEFAULT_FACTORIES["tools"] and not modules["tools"].options:
        modules["tools"] = ModuleSpec(modules["tools"].factory, {"root": str(Path.cwd())})
    providers, _ = providers_for(modules["core"])
    saved = read_saved(data_dir / SETTINGS_FILE, providers)
    if "provider" in saved:
        chosen = saved.pop("provider")
        modules["core"] = next(p.core for p in providers if p.id == chosen)
    if "work_folder" in saved:
        tools = modules["tools"]
        modules["tools"] = ModuleSpec(tools.factory, {**tools.options,
                                                      "root": saved.pop("work_folder")})
    runtime.update(saved)
    for key in ("model", "maintenance_model"):
        if getattr(args, key):
            runtime[key] = getattr(args, key)
    if args.permissions:
        runtime["permissions"] = PermissionPolicy(args.permissions)
    if args.mode:
        start = {"mode": args.mode, "activation": args.activation}
        if args.conversation:
            start["conversation_id"] = args.conversation
        frontend = modules["frontend"]
        modules["frontend"] = ModuleSpec(frontend.factory, {**frontend.options, **start})
    _, provider = providers_for(modules["core"])
    return InstanceConfig(
        runtime=RuntimeConfig(data_dir=data_dir, providers=tuple(providers),
                              provider=provider, tools_module=modules["tools"], **runtime),
        **modules,
    )


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    config = load_config(args)
    if args.show_config:
        print(show_config(config, config_path(args)), end="")
        return
    try:
        asyncio.run(run_instance(config))
    except ModuleLoadError as e:
        print(f"kayden: {e}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
