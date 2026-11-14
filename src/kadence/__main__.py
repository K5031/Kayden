"""Run Kadence in a terminal: kadence [options] (or python -m kadence).

Builds one instance from an InstanceConfig. A TOML config file (--config, else
<data dir>/config.toml) names the modules and the per-instance runtime settings (model,
maintenance model, permissions, budget, system prompt); the settings saved from the app
(<data dir>/settings.toml) and then command-line flags override it. Anything they leave out
is a default: SQLite Memory, the Anthropic Core, shell/file Tools rooted at the current
directory, the Textual TUI and no budget. --show-config prints the result.

The saved frontend runs here when it is a terminal frontend. A saved window frontend opens
instead: kadence hands over to the window launcher as a detached process when a display is
available, and runs the TUI (saying why) when there is none or the frontend is not
installed. Restart now in a frontend starts the instance again in this process, or hands
over the same way to a window frontend.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import subprocess
import sys
import tomllib
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import tomli_w

from kadence.api import MODULE_KINDS, Budget, McpServer, PermissionPolicy
from kadence.runtime import (
    InstanceConfig,
    ModuleLoadError,
    ModuleSpec,
    ModuleState,
    Restart,
    RuntimeConfig,
    run_instance,
)
from kadence.runtime.modules import (
    LABELS,
    MCP_FACTORY,
    TERMINAL,
    WINDOW,
    find,
    has_display,
    mcp_key,
    targets,
)
from kadence.runtime.settings import SETTINGS_FILE, read_secret

DEFAULT_DATA_DIR = Path("~/.kadence")
CONFIG_FILE = "config.toml"
DEFAULT_FACTORIES = {
    "core": "kadence.core.anthropic:factory",
    "memory": "kadence.memory.sqlite:factory",
    "tools": "kadence.tools.shell_files:factory",
    "frontend": "kadence.frontends.tui:factory",
}
RUNTIME_KEYS = ("model", "maintenance_model", "permissions", "budget", "system_prompt",
                "system_prompt_file")
SECRET_WORDS = ("key", "token", "secret", "password")
# The frontend each launcher falls back to: its own kind's built-in one.
LAUNCHER_FRONTENDS = {TERMINAL: "tui", WINDOW: "desktop"}
# Not an installed frontend: a person never chooses it in Settings; --headless runs it.
HEADLESS_FACTORY = "kadence.frontends.headless:factory"
SAVED_KEYS = ("modules", "model", "maintenance_model", "permissions",
              "system_prompt", "work_folder", "budget", "mcp_servers")
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
    header = ["# Effective Kadence config", f"# data dir: {config.runtime.data_dir}",
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


@dataclass(frozen=True, slots=True)
class Launch:
    """What a launcher was started with: each sets the fields it has flags for."""

    data_dir: Path
    config: Path | None = None
    conversation: str | None = None
    model: str | None = None
    maintenance_model: str | None = None
    permissions: str | None = None
    headless: bool = False
    duty: str | None = None
    goal: str | None = None
    show_config: bool = False


def parse_args(argv: Sequence[str] | None = None) -> Launch:
    parser = argparse.ArgumentParser(prog="kadence", description=__doc__)
    parser.add_argument("--data-dir", type=Path,
                        default=Path(os.environ.get("KADENCE_DATA_DIR", DEFAULT_DATA_DIR)),
                        help="instance data directory (default: $KADENCE_DATA_DIR or ~/.kadence)")
    parser.add_argument("--config", type=Path,
                        help=f"config file (default: <data dir>/{CONFIG_FILE})")
    parser.add_argument("--conversation",
                        help="start right away in this conversation (default: ask)")
    parser.add_argument("--model", help="model id (default: the first the Core lists)")
    parser.add_argument("--maintenance-model", help="cheaper model for memory maintenance")
    parser.add_argument("--permissions", choices=[p.value for p in PermissionPolicy],
                        help="tool permission policy (default: auto)")
    parser.add_argument("--headless", action="store_true",
                        help="be driven by another program: JSON-RPC on stdin and stdout")
    duty = parser.add_mutually_exclusive_group()
    duty.add_argument("--duty", help="with --headless: start a new conversation on this duty")
    duty.add_argument("--goal", help="with --headless: start a new conversation on this goal")
    parser.add_argument("--show-config", action="store_true",
                        help="print the effective config as TOML and exit")
    return Launch(**vars(parser.parse_args(argv)))


def start_options(args: Launch) -> dict[str, Any] | None:
    """Frontend options that start right away, or None to let the frontend ask."""
    if args.duty is not None or args.goal is not None:
        if not args.headless:
            raise SystemExit("kadence: --duty and --goal need --headless")
        if args.conversation is not None:
            raise SystemExit("kadence: a duty starts a new conversation; leave out --conversation")
        return {"start": True, "duty": args.duty or args.goal, "goal": args.goal is not None}
    if args.conversation is None:
        return None
    return {"start": True, "conversation_id": args.conversation}


def config_path(args: Launch) -> Path:
    if args.config:
        return args.config.expanduser()
    return args.data_dir.expanduser() / CONFIG_FILE


def read_saved(path: Path) -> dict[str, Any]:
    """Settings saved by RuntimeAPI.update_settings, validated like [runtime]. A model of
    "" is the Core's first, a maintenance model of "" the main model."""
    data = read_config(path, required=False, sections=SAVED_KEYS)
    if not isinstance(data.get("work_folder", ""), str):
        raise SystemExit(f"{path}: work_folder must be a string")
    modules = data.get("modules", {})
    if not isinstance(modules, dict) or not all(
            kind in MODULE_KINDS and isinstance(v, str) for kind, v in modules.items()):
        raise SystemExit(f"{path}: [modules] maps {', '.join(MODULE_KINDS)} to module ids")
    servers = data.get("mcp_servers", [])
    try:
        servers = tuple(McpServer(s["name"], s.get("command"), s.get("url"),
                                  dict.fromkeys(s.get("env", []), "")) for s in servers)
    except (TypeError, KeyError, AttributeError):
        raise SystemExit(f"{path}: each [[mcp_servers]] has a name, a command or url, and "
                         "env, a list of variable names") from None
    runtime = {k: v for k, v in data.items()
               if k not in ("modules", "work_folder", "mcp_servers")}
    cleared = [k for k in ("model", "maintenance_model") if runtime.get(k) == ""]
    for key in cleared:
        del runtime[key]
    saved = load_runtime(path, {"runtime": runtime})
    saved.update(dict.fromkeys(cleared))
    if modules:
        saved["modules"] = modules
    if "work_folder" in data:
        saved["work_folder"] = data["work_folder"]
    saved["mcp_servers"] = servers
    return saved


def module_id(kind: str, spec: ModuleSpec) -> str | None:
    """The id of the installed module whose factory `spec` names; None for any other."""
    return next((i for i, target in targets(kind).items() if target == spec.factory), None)


def choose_frontend(chosen: str | None, configured: ModuleSpec, launcher: str
                    ) -> tuple[str | None, ModuleSpec]:
    """The frontend this launcher runs: the chosen one if it is installed and of the
    launcher's kind, else the launcher's own."""
    if chosen is None and module_id("frontend", configured) is None:
        return None, configured  # an import path from config.toml
    info = find("frontend", chosen)
    if info is None or info.kind != launcher:
        own = LAUNCHER_FRONTENDS[launcher]
        return own, ModuleSpec(targets("frontend")[own])
    return info.id, (configured if info.factory == configured.factory
                     else ModuleSpec(info.factory))


def load_config(args: Launch, *, launcher: str = TERMINAL,
                restart: Restart | None = None) -> InstanceConfig:
    """config.toml, then the saved settings, then command-line flags, each overriding.
    The Tools root defaults to the current directory. After a
    `restart` its frontend and conversation replace the command-line start options."""
    data_dir = args.data_dir.expanduser()
    path = config_path(args)
    data = read_config(path, required=args.config is not None)
    modules = load_modules(path, data)
    runtime = load_runtime(path, data)
    saved = read_saved(data_dir / SETTINGS_FILE)
    chosen = {kind: module_id(kind, spec) for kind, spec in modules.items()}
    chosen.update(saved.pop("modules", {}))
    running: dict[str, str | None] = {}
    problems = []
    for kind in ("core", "memory", "tools"):
        running[kind] = module_id(kind, modules[kind])
        if chosen[kind] == running[kind]:
            continue
        if (target := targets(kind).get(chosen[kind] or "")) is None:
            problems.append(f"the {LABELS[kind]} module {chosen[kind]!r} is not installed; "
                            "choose another one in Settings")
            continue
        modules[kind], running[kind] = ModuleSpec(target), chosen[kind]
    want = restart.frontend if restart is not None else chosen["frontend"]
    if args.headless:
        running["frontend"], modules["frontend"] = None, ModuleSpec(HEADLESS_FACTORY)
    else:
        running["frontend"], modules["frontend"] = choose_frontend(want, modules["frontend"],
                                                                   launcher)

    tools = modules["tools"]
    if running["tools"] == "shell-files" and "root" not in tools.options:
        tools = ModuleSpec(tools.factory, {**tools.options, "root": str(Path.cwd())})
    if "work_folder" in saved and "root" in tools.options:
        tools = ModuleSpec(tools.factory, {**tools.options, "root": saved["work_folder"]})
    saved.pop("work_folder", None)
    modules["tools"] = tools
    servers = saved.pop("mcp_servers")
    extra = (ModuleSpec(MCP_FACTORY, {"servers": [mcp_options(s) for s in servers]}),
             ) if servers else ()
    runtime.update(saved)
    for key in ("model", "maintenance_model"):
        if getattr(args, key):
            runtime[key] = getattr(args, key)
    if args.permissions:
        runtime["permissions"] = PermissionPolicy(args.permissions)
    if restart is not None:
        start = ({"start": True, "conversation_id": restart.conversation_id}
                 if restart.conversation_id is not None else None)
    else:
        start = start_options(args)
    if start is not None:
        frontend = modules["frontend"]
        modules["frontend"] = ModuleSpec(frontend.factory, {**frontend.options, **start})
    state = ModuleState(running=running, chosen=chosen, work_folder=tools.options.get("root"),
                        mcp_servers=servers)
    return InstanceConfig(
        runtime=RuntimeConfig(data_dir=data_dir, modules=state, **runtime),
        startup_error="; ".join(problems) or None,
        extra_tools=extra,
        **modules,
    )


def mcp_options(server: McpServer) -> dict[str, Any]:
    """A server as the MCP Tools module takes it, its variables read from the keychain."""
    target = {"command": server.command} if server.command else {"url": server.url}
    env = {v: value for v in server.env if (value := read_secret(mcp_key(server.name, v)))}
    return {"name": server.name, **target, "env": env}


type EventLoop = Callable[[Coroutine[Any, Any, Any]], Any]


def frontend_loop(spec: ModuleSpec) -> EventLoop:
    """How a frontend runs: its factory's event_loop, else asyncio.run. Imports the factory
    without calling it."""
    module_name, _, attr = spec.factory.partition(":")
    factory: Any = importlib.import_module(module_name)
    for part in attr.split("."):
        factory = getattr(factory, part)
    return getattr(factory, "event_loop", asyncio.run)


async def launch(args: Launch, launcher: str, *, restart: Restart | None = None,
                 runs_here: Callable[[InstanceConfig], bool] = lambda config: True,
                 ) -> Restart | None:
    """Run instances until the user quits. A Restart is returned for the caller when it is
    to a frontend of another kind, or to one that `runs_here` says needs another loop."""
    while True:
        config = load_config(args, launcher=launcher, restart=restart)
        if not runs_here(config):
            return restart
        restart = await run_instance(config, fallback=LAUNCHER_FRONTENDS[launcher])
        if restart is None or restart.frontend_kind != launcher:
            return restart


async def _fail(error: SystemExit) -> None:
    raise error


def run_frontends(args: Launch, launcher: str) -> Restart | None:
    """Run instances, each frontend on the event loop it names, until the user quits or
    a Restart asks for a frontend of the other kind (returned)."""
    restart: Restart | None = None
    while True:
        try:
            config = load_config(args, launcher=launcher, restart=restart)
        except SystemExit as e:
            # A bad config file, before any frontend is chosen: the launcher's own
            # frontend says so on its loop (a window's stderr is unseen).
            own = ModuleSpec(targets("frontend")[LAUNCHER_FRONTENDS[launcher]])
            frontend_loop(own)(_fail(e))
            raise
        run = frontend_loop(config.frontend)
        restart = run(launch(args, launcher, restart=restart,
                             runs_here=lambda c, run=run: frontend_loop(c.frontend) is run))
        if restart is None or restart.frontend_kind != launcher:
            return restart


def window_command(args: Launch, restart: Restart) -> list[str]:
    """The window launcher on the same data directory, config file and conversation."""
    command = [sys.executable, "-m", "kadence.window",
               "--data-dir", str(args.data_dir.expanduser())]
    if args.config:
        command += ["--config", str(args.config.expanduser())]
    if restart.conversation_id is not None:
        command += ["--conversation", restart.conversation_id]
    return command


def hand_over(args: Launch, restart: Restart, *,
              message: str = "Kadence continues in the desktop app.") -> None:
    """Start the desktop app detached from this terminal."""
    subprocess.Popen(window_command(args, restart), start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    print(message)


def saved_window(config: InstanceConfig) -> tuple[str | None, str | None]:
    """The saved frontend `kadence` hands over to at launch (a window frontend, with a
    display), or the line saying why it runs in the terminal instead; (None, None) when the
    saved frontend runs here."""
    state = config.runtime.modules
    chosen = state.chosen.get("frontend")
    if chosen is None or chosen == state.running.get("frontend"):
        return None, None
    info = find("frontend", chosen)
    if info is None:
        return None, f"The interface {chosen!r} is not installed, so Kadence opens in the terminal."
    if not has_display():
        return None, (f"No display here, so Kadence opens in the terminal. {info.name} opens "
                      "when you start Kadence on a computer with a display.")
    return info.id, None


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    config = load_config(args)
    if args.show_config:
        print(show_config(config, config_path(args)), end="")
        return
    window, notice = (None, None) if args.headless else saved_window(config)
    if window is not None:
        hand_over(args, Restart(window, WINDOW, args.conversation),
                  message="Kadence opens in the desktop app.")
        return
    if notice is not None:
        print(notice)
    try:
        restart = run_frontends(args, TERMINAL)
    except ModuleLoadError as e:
        print(f"kadence: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    if restart is not None:
        hand_over(args, restart)


if __name__ == "__main__":
    main()
