from interfaces import ToolRuntimeInterface
from events import ToolResult
from klogger import get_logger


class ToolRuntime(ToolRuntimeInterface):
    def __init__(self, modules):
        self.log = get_logger("Tools")
        self._modules = list(modules)
        self._schemas = []
        self._owners = {}

        for module in self._modules:
            schemas = module.schemas()
            if not isinstance(schemas, list):
                raise ValueError(f"tool schemas must be a list: {module!r}")
            for schema in schemas:
                try:
                    name = schema["function"]["name"]
                except (KeyError, TypeError):
                    raise ValueError(f"invalid tool schema from {module!r}")
                if not isinstance(name, str) or not name:
                    raise ValueError(f"invalid tool name from {module!r}")
                if name in self._owners:
                    raise ValueError(f"duplicate tool name: {name}")
                self._schemas.append(schema)
                self._owners[name] = module

    def schemas(self) -> list[dict]:
        return list(self._schemas)

    def execute(self, name: str, arguments: dict) -> ToolResult:
        if not isinstance(arguments, dict):
            return ToolResult("Tool error: arguments must be an object", is_error=True)
        module = self._owners.get(name)
        if module is None:
            return ToolResult(f"Tool error: unknown tool '{name}'", is_error=True)
        try:
            return ToolResult(str(module.execute(name, arguments)))
        except (ValueError, TypeError, KeyError) as error:
            self.log.warning("Tool %s failed: %s", name, error)
            return ToolResult(f"Tool error: {error}", is_error=True)
        except Exception:
            self.log.exception("Tool %s failed", name)
            return ToolResult("Tool error: execution failed", is_error=True)

    def stop(self) -> None:
        for module in reversed(self._modules):
            stop = getattr(module, "stop", None)
            if stop:
                try:
                    stop()
                except Exception:
                    self.log.exception("Failed to stop tool module: %s", module)
