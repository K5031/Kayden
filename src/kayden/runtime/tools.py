import threading

from kayden.interfaces import ToolRuntimeInterface
from kayden.events import ToolResult
from kayden.klogger import get_logger
from kayden.permissions import OperationDefinition, PermissionDenied


class ToolRuntime(ToolRuntimeInterface):
    def __init__(self, modules):
        self.log = get_logger("Tools")
        self._modules = list(modules)
        self._borrowed = []
        self._schemas = []
        self._owners = {}
        self._lock = threading.RLock()
        self._authorizer = None

        for module in self._modules:
            self._register(module)

    def _register(self, module) -> None:
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

    @staticmethod
    def _definitions(modules):
        schemas = []
        owners = {}
        for module in modules:
            module_schemas = module.schemas()
            if not isinstance(module_schemas, list):
                raise ValueError(f"tool schemas must be a list: {module!r}")
            for schema in module_schemas:
                try:
                    name = schema["function"]["name"]
                except (KeyError, TypeError):
                    raise ValueError(f"invalid tool schema from {module!r}")
                if not isinstance(name, str) or not name:
                    raise ValueError(f"invalid tool name from {module!r}")
                if name in owners:
                    raise ValueError(f"duplicate tool name: {name}")
                schemas.append(schema)
                owners[name] = module
        return schemas, owners

    def _replace_borrowed(self, borrowed) -> None:
        schemas, owners = self._definitions([*self._modules, *borrowed])
        self._borrowed = list(borrowed)
        self._schemas = schemas
        self._owners = owners

    def add(self, module) -> None:
        """Expose tools from another capability without taking ownership of it."""
        with self._lock:
            self._replace_borrowed([*self._borrowed, module])

    def replace_borrowed(self, old, new) -> None:
        """Atomically replace tools borrowed from another capability."""
        with self._lock:
            borrowed = [module for module in self._borrowed if module is not old]
            if new is not None:
                borrowed.append(new)
            self._replace_borrowed(borrowed)

    def schemas(self) -> list[dict]:
        with self._lock:
            return list(self._schemas)

    def bind_authorizer(self, authorizer) -> None:
        """Bind the runtime-owned authorization boundary for tool execution."""
        if authorizer is not None and not callable(authorizer):
            raise TypeError("tool authorizer must be callable")
        with self._lock:
            self._authorizer = authorizer

    def execute(self, name: str, arguments: dict) -> ToolResult:
        if not isinstance(arguments, dict):
            return ToolResult("Tool error: arguments must be an object", is_error=True)
        with self._lock:
            module = self._owners.get(name)
            authorizer = self._authorizer
        if module is None:
            return ToolResult(f"Tool error: unknown tool '{name}'", is_error=True)
        try:
            describe_operation = getattr(module, "operation", None)
            operation = (
                describe_operation(name, arguments)
                if callable(describe_operation)
                else OperationDefinition(
                    id=f"tool.{name}",
                    label=" ".join(name.split("_")).capitalize(),
                )
            )
            if not isinstance(operation, OperationDefinition):
                raise TypeError("tool operation metadata is invalid")
            if authorizer is not None:
                authorizer(operation, arguments)
            return ToolResult(str(module.execute(name, arguments)))
        except PermissionDenied as error:
            self.log.info("Tool %s denied by runtime policy", name)
            return ToolResult(f"Tool error: {error}", is_error=True)
        except (ValueError, TypeError, KeyError) as error:
            self.log.debug("Tool %s failed: %s", name, error)
            return ToolResult(f"Tool error: {error}", is_error=True)
        except Exception:
            self.log.exception("Tool %s failed", name)
            return ToolResult("Tool error: execution failed", is_error=True)

    def describe_activity(
        self,
        name: str,
        arguments: dict,
        state: str,
        result: ToolResult | None = None,
    ) -> str:
        with self._lock:
            module = self._owners.get(name)
        description = None
        describe = getattr(module, "describe_activity", None)
        if callable(describe):
            try:
                description = describe(name, arguments, state, result)
            except Exception:
                self.log.exception("Tool %s failed to describe its activity", name)
        if not isinstance(description, str) or not description.strip():
            description = super().describe_activity(name, arguments, state, result)
        description = " ".join(description.split())
        if state == "running" and not description.endswith(("…", "...")):
            description += "…"
        if state == "failed" and result is not None:
            detail = " ".join(
                result.content.removeprefix("Tool error: ").split()
            )[:120]
            if detail and detail.lower() not in description.lower():
                description += f" — {detail}"
        return description[:240]

    def contributions(self):
        with self._lock:
            modules = list(self._modules)
        contributed = []
        for module in modules:
            provide = getattr(module, "contributions", None)
            if provide:
                contributed.extend(provide() or [])
        return contributed

    def stop(self) -> None:
        with self._lock:
            modules = list(self._modules)
        for module in reversed(modules):
            stop = getattr(module, "stop", None)
            if stop:
                try:
                    stop()
                except Exception:
                    self.log.exception("Failed to stop tool module: %s", module)
