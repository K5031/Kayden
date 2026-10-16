import importlib
from config import load_config
from interfaces import (
    ActionInterface,
    ContextInterface,
    ControllerInterface,
    CoreInterface,
    InputInterface,
    MemoryInterface,
    OutputInterface,
    ToolInterface,
)
from klogger import get_logger
from tool_runtime import ToolRuntime

log = get_logger("Registry")

INTERFACES = {
    "controller": ControllerInterface,
    "input": InputInterface,
    "output": OutputInterface,
    "core": CoreInterface,
    "context": ContextInterface,
    "memory": MemoryInterface,
    "action": ActionInterface,
    "tools": ToolInterface,
}


def _load_module(capability: str, name: str):
    import_name = f"modules.{capability}.{name}"
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

        keys = list(config_values.keys())
        if not keys or keys[0] != "controller":
            raise ValueError(
                f"config must have 'controller' as its first key, got: {keys}"
            )

        controller_name = config_values["controller"]
        if not isinstance(controller_name, str) or not controller_name:
            raise TypeError("controller must name one module")
        controller_mod = _load_module("controller", controller_name)
        if not issubclass(controller_mod.Module, ControllerInterface):
            raise TypeError(f"controller module '{controller_name}' has the wrong interface")
        required = set(controller_mod.Module.required_modules)
        optional = set(controller_mod.Module.optional_modules)
        provided = set(config_values.keys()) - {"controller"}

        missing = required - provided
        extra = provided - required - optional
        if missing or extra:
            problems = []
            if missing:
                problems.append(f"missing: {sorted(missing)}")
            if extra:
                problems.append(f"unexpected: {sorted(extra)}")
            raise ValueError(
                f"config does not match controller '{controller_name}' requirements — "
                + ", ".join(problems)
            )

        module_classes = {"controller": [(controller_name, controller_mod.Module)]}
        for capability, configured_names in config_values.items():
            if capability == "controller":
                continue
            if capability not in INTERFACES:
                raise ValueError(f"unknown capability: {capability}")
            names = configured_names if isinstance(configured_names, list) else [configured_names]
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
                    raise TypeError(f"{capability} module '{name}' has the wrong interface")
                module_classes[capability].append((name, mod.Module))

        for capability, features in controller_mod.Module.required_features.items():
            for name, module_class in module_classes[capability]:
                for feature, expected in features.items():
                    actual = getattr(module_class, feature, None)
                    if actual != expected:
                        raise ValueError(
                            f"{capability} module '{name}' is incompatible with controller "
                            f"'{controller_name}': {feature} must be {expected!r}, got {actual!r}"
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
