import importlib
from kayden.config import load_config
from kayden.interfaces import (
    ActionInterface,
    ContextPolicyInterface,
    CoreInterface,
    FrontendInterface,
    InputInterface,
    MemoryInterface,
    OutputInterface,
    ToolInterface,
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


class Registry:
    def __init__(self):
        self._registry = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        for capability, module in reversed(list(self._registry.items())):
            if hasattr(module, "stop"):
                log.info("Stopping module: %s (%s)", capability, module)
                try:
                    module.stop()
                except Exception:
                    log.exception("Failed to stop module: %s", capability)
        self._registry.clear()

    def register(self, capability: str, module):
        self._registry[capability] = module

    def get(self, capability: str):
        try:
            return self._registry[capability]
        except KeyError:
            raise KeyError(f"No module registered for capability '{capability}'")

    def has(self, capability: str) -> bool:
        return capability in self._registry

    @classmethod
    def from_config(cls, config_values: dict = None) -> "Registry":
        config_values = load_config() if config_values is None else config_values
        if not isinstance(config_values, dict):
            raise TypeError("config must be a TOML table")

        # Controller modules existed before Kayden's application lifecycle became
        # fixed. Ignore the old key so existing user configs migrate in place.
        config_values = {
            capability: value
            for capability, value in config_values.items()
            if capability != "controller"
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

        api = cls()
        try:
            for capability in config_values:
                classes = module_classes[capability]
                instances = []
                try:
                    for name, module_class in classes:
                        log.info("Loading %s: %s", capability, name)
                        instances.append(module_class())
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
                api.register(capability, instance)
        except Exception:
            api.close()
            raise
        return api
