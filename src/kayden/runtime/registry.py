import importlib
import pkgutil
import threading
from kayden.config import CONFIG_PATH, load_config
from kayden.interfaces import (
    ActionInterface,
    ContextPolicyInterface,
    CoreInterface,
    FrontendInterface,
    InputInterface,
    MemoryInterface,
    OutputInterface,
    ToolInterface,
    ToolRuntimeInterface,
)
from kayden.klogger import get_logger
from kayden.runtime.tools import ToolRuntime

log = get_logger("Registry")

INTERFACES = {
    "frontend": FrontendInterface,
    "input": InputInterface,
    "output": OutputInterface,
    "core": CoreInterface,
    "context": ContextPolicyInterface,
    "memory": MemoryInterface,
    "action": ActionInterface,
    "tools": ToolInterface,
}

REQUIRED_CAPABILITIES = {
    "frontend",
    "core",
    "context",
    "memory",
    "action",
    "tools",
}
OPTIONAL_CAPABILITIES = {"input", "output"}
MODULE_NAME_PARTS = {
    "ai": "AI",
    "chatgpt": "ChatGPT",
    "cpp": "C++",
    "deepseek": "DeepSeek",
    "exa": "Exa",
    "gpt": "GPT",
    "openai": "OpenAI",
    "qt": "Qt",
    "searxng": "SearXNG",
    "tui": "TUI",
}


def _module_display_name(name: str) -> str:
    return " ".join(
        MODULE_NAME_PARTS.get(part, part.title())
        for part in name.split("_")
    )


def _load_module(capability: str, name: str):
    import_name = f"kayden.modules.{capability}.{name}"
    try:
        return importlib.import_module(import_name)
    except ModuleNotFoundError as error:
        if error.name != import_name:
            raise ModuleNotFoundError(
                f"cannot load {capability} module '{name}': missing dependency "
                f"'{error.name}'. Install the corresponding optional dependency group."
            ) from error
        raise


class _MultiModule:
    def __init__(self, instances):
        self._instances = instances

    def __getattr__(self, name):
        matching = [inst for inst in self._instances if hasattr(inst, name)]
        if not matching:
            raise AttributeError(name)

        def call_all(*args, **kwargs):
            results = [getattr(inst, name)(*args, **kwargs) for inst in matching]
            return results[0] if results else None

        return call_all

    def stop(self):
        for instance in reversed(self._instances):
            stop = getattr(instance, "stop", None)
            if stop:
                try:
                    stop()
                except Exception:
                    log.exception("Failed to stop module instance: %s", instance)

    def contributions(self):
        contributed = []
        for instance in self._instances:
            provide = getattr(instance, "contributions", None)
            if provide:
                contributed.extend(provide() or [])
        return contributed


class Registry:
    def __init__(self, assistant_name="Kayden"):
        self._registry = {}
        self._selections = {}
        self._lock = threading.RLock()
        self.assistant_name = assistant_name
        self.module_settings = {}
        self.config_path = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        with self._lock:
            registered = list(self._registry.items())
            self._registry.clear()
        for capability, module in reversed(registered):
            if hasattr(module, "stop"):
                log.info("Stopping module: %s (%s)", capability, module)
                try:
                    module.stop()
                except Exception:
                    log.exception("Failed to stop module: %s", capability)
    def register(self, capability: str, module, selection=None):
        with self._lock:
            self._registry[capability] = module
            if selection is not None:
                self._selections[capability] = (
                    list(selection) if isinstance(selection, list) else selection
                )

    def selection(self, capability: str):
        with self._lock:
            selected = self._selections.get(capability, [])
            return list(selected) if isinstance(selected, list) else selected

    def set_selections(self, selections: dict) -> None:
        with self._lock:
            for capability, selected in selections.items():
                self._selections[capability] = (
                    list(selected) if isinstance(selected, list) else selected
                )

    @classmethod
    def available_modules(cls, capability: str) -> list[dict[str, str]]:
        """Discover installed module names without importing their dependencies."""
        if capability not in INTERFACES:
            raise ValueError(f"unknown capability: {capability}")
        package = importlib.import_module(f"kayden.modules.{capability}")
        discovered = []
        for item in pkgutil.iter_modules(package.__path__):
            if item.name.startswith("_"):
                continue
            label = _module_display_name(item.name)
            discovered.append({"id": item.name, "name": label})
        return sorted(discovered, key=lambda item: item["name"].casefold())

    def get(self, capability: str):
        with self._lock:
            try:
                return self._registry[capability]
            except KeyError:
                raise KeyError(f"No module registered for capability '{capability}'")

    def has(self, capability: str) -> bool:
        with self._lock:
            return capability in self._registry

    @staticmethod
    def _matches_interface(capability, module) -> bool:
        if capability == "tools":
            return isinstance(module, ToolRuntimeInterface)
        if isinstance(module, _MultiModule):
            return all(
                isinstance(instance, INTERFACES[capability])
                for instance in module._instances
            )
        return isinstance(module, INTERFACES[capability])

    def swap(self, replacements: dict) -> dict:
        """Atomically publish prepared modules and return the displaced modules."""
        if not isinstance(replacements, dict) or not replacements:
            raise TypeError("replacements must be a non-empty mapping")
        with self._lock:
            prospective = dict(self._registry)
            for capability, module in replacements.items():
                if capability not in INTERFACES:
                    raise ValueError(f"unknown capability: {capability}")
                if module is None:
                    if capability not in OPTIONAL_CAPABILITIES:
                        raise ValueError(f"cannot remove required capability: {capability}")
                    prospective.pop(capability, None)
                    continue
                if not self._matches_interface(capability, module):
                    raise TypeError(
                        f"replacement for '{capability}' has the wrong interface"
                    )
                prospective[capability] = module

            missing = REQUIRED_CAPABILITIES - set(prospective)
            if missing:
                raise ValueError(f"missing required capabilities: {sorted(missing)}")
            core = prospective["core"]
            context = prospective["context"]
            expected_usage = "reported" if core.reports_usage else "self"
            actual_usage = getattr(context, "usage_mode", None)
            if actual_usage != expected_usage:
                raise ValueError(
                    "replacement core and context are incompatible: "
                    f"usage_mode must be {expected_usage!r}, got {actual_usage!r}"
                )

            displaced = {
                capability: self._registry.get(capability)
                for capability in replacements
            }
            self._registry = prospective
            return displaced

    @classmethod
    def prepare(cls, capability: str, configured_names, options=None):
        """Construct and validate a module without publishing it."""
        if capability not in INTERFACES:
            raise ValueError(f"unknown capability: {capability}")
        names = (
            configured_names
            if isinstance(configured_names, list)
            else [configured_names]
        )
        if not names and capability != "tools":
            raise TypeError(f"{capability} must name one or more modules")
        if any(not isinstance(name, str) or not name for name in names):
            raise TypeError(f"{capability} must name one or more modules")
        if len(names) > 1 and capability not in {"output", "tools"}:
            raise ValueError("only output and tools support more than one module")
        if options is None:
            options = {}
        if not isinstance(options, dict):
            raise TypeError("module options must be a mapping")

        instances = []
        try:
            for name in names:
                module = _load_module(capability, name)
                if not issubclass(module.Module, INTERFACES[capability]):
                    raise TypeError(
                        f"{capability} module '{name}' has the wrong interface"
                    )
                module_options = (
                    options.get(name, {})
                    if len(names) > 1
                    else options
                )
                if not isinstance(module_options, dict):
                    raise TypeError(f"options for module '{name}' must be a mapping")
                instances.append(module.Module(**module_options))
        except Exception:
            for instance in reversed(instances):
                stop = getattr(instance, "stop", None)
                if stop:
                    try:
                        stop()
                    except Exception:
                        log.exception("Failed to stop prepared module: %s", instance)
            raise

        if capability == "tools":
            return ToolRuntime(instances)
        if len(instances) > 1:
            return _MultiModule(instances)
        return instances[0]

    @classmethod
    def from_config(cls, config_values: dict = None) -> "Registry":
        loaded_from_disk = config_values is None
        config_values = load_config() if loaded_from_disk else config_values
        if not isinstance(config_values, dict):
            raise TypeError("config must be a TOML table")

        module_settings = config_values.get("settings", {})
        if not isinstance(module_settings, dict):
            raise TypeError("settings must be a TOML table")
        assistant = config_values.get("assistant", {})
        if not isinstance(assistant, dict):
            raise TypeError("assistant must be a TOML table")
        unknown_assistant_settings = set(assistant) - {"name"}
        if unknown_assistant_settings:
            raise ValueError(
                f"unknown assistant settings: {sorted(unknown_assistant_settings)}"
            )
        assistant_name = assistant.get("name", "Kayden")
        if not isinstance(assistant_name, str) or not assistant_name.strip():
            raise TypeError("assistant.name must be non-empty text")
        assistant_name = assistant_name.strip()
        if len(assistant_name) > 64 or any(ord(char) < 32 for char in assistant_name):
            raise ValueError("assistant.name must be a single line of at most 64 characters")

        # Controller modules existed before Kayden's application lifecycle became
        # fixed. Ignore the old key so existing user configs migrate in place.
        config_values = {
            capability: value
            for capability, value in config_values.items()
            if capability not in {"assistant", "controller", "settings"}
        }
        provided = set(config_values)
        missing = REQUIRED_CAPABILITIES - provided
        extra = provided - REQUIRED_CAPABILITIES - OPTIONAL_CAPABILITIES
        if missing or extra:
            problems = []
            if missing:
                problems.append(f"missing: {sorted(missing)}")
            if extra:
                problems.append(f"unexpected: {sorted(extra)}")
            raise ValueError(
                "config does not match Kayden requirements — "
                + ", ".join(problems)
            )

        module_classes = {}
        for capability, configured_names in config_values.items():
            if capability not in INTERFACES:
                raise ValueError(f"unknown capability: {capability}")
            names = (
                configured_names
                if isinstance(configured_names, list)
                else [configured_names]
            )
            if not names and capability != "tools":
                raise TypeError(f"{capability} must name one or more modules")
            if any(not isinstance(name, str) or not name for name in names):
                raise TypeError(f"{capability} must name one or more modules")
            if len(names) > 1 and capability not in {"output", "tools"}:
                raise ValueError("only output and tools support more than one module")
            if capability == "action" and len(names) != 1:
                raise ValueError("action must name one module; it may expose multiple tools")

            module_classes[capability] = []
            for name in names:
                mod = _load_module(capability, name)
                if not issubclass(mod.Module, INTERFACES[capability]):
                    raise TypeError(
                        f"{capability} module '{name}' has the wrong interface"
                    )
                module_classes[capability].append((name, mod.Module))

        core_name, core_class = module_classes["core"][0]
        context_name, context_class = module_classes["context"][0]
        expected_usage = "reported" if core_class.reports_usage else "self"
        actual_usage = getattr(context_class, "usage_mode", None)
        if actual_usage != expected_usage:
            raise ValueError(
                f"core module '{core_name}' is incompatible with context module "
                f"'{context_name}': usage_mode must be {expected_usage!r}, "
                f"got {actual_usage!r}"
            )

        api = cls(assistant_name=assistant_name)
        api.module_settings = module_settings
        api.config_path = CONFIG_PATH if loaded_from_disk else None
        try:
            for capability in config_values:
                classes = module_classes[capability]
                instances = []
                try:
                    for name, module_class in classes:
                        log.info("Loading %s: %s", capability, name)
                        capability_settings = module_settings.get(capability, {})
                        if not isinstance(capability_settings, dict):
                            raise TypeError(
                                f"settings.{capability} must be a TOML table"
                            )
                        options = capability_settings.get(name, {})
                        if not isinstance(options, dict):
                            raise TypeError(
                                f"settings.{capability}.{name} must be a TOML table"
                            )
                        instances.append(module_class(**options))
                    if capability == "tools":
                        instance = ToolRuntime(instances)
                    else:
                        instance = _MultiModule(instances) if len(instances) > 1 else instances[0]
                except Exception:
                    for pending in reversed(instances):
                        stop = getattr(pending, "stop", None)
                        if stop:
                            try:
                                stop()
                            except Exception:
                                log.exception("Failed to stop partial module: %s", pending)
                    raise
                configured_selection = config_values[capability]
                selection = (
                    list(configured_selection)
                    if isinstance(configured_selection, list)
                    else configured_selection
                )
                api.register(capability, instance, selection=selection)
        except Exception:
            api.close()
            raise
        return api
