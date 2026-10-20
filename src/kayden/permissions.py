"""Runtime-owned authorization for cooperative Kayden operations."""

from copy import deepcopy
from dataclasses import dataclass, field
import itertools
import threading
from typing import Callable, Mapping


DECISIONS = {"allow", "ask", "deny"}
ORIGINS = {"user", "model", "system"}
PREVIEW_KINDS = {"diff", "text"}


@dataclass(frozen=True)
class OperationPreview:
    """Provider-generated presentation of the exact proposed operation."""

    kind: str
    content: str
    title: str = "Preview"

    def __post_init__(self):
        if self.kind not in PREVIEW_KINDS:
            raise ValueError(f"unsupported operation preview kind: {self.kind}")
        if not isinstance(self.content, str) or not self.content:
            raise ValueError("operation preview content must be non-empty text")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("operation preview title must be non-empty text")


@dataclass(frozen=True)
class OperationDefinition:
    """Provider metadata describing an operation, not proof that it is safe."""

    id: str
    label: str
    effects: frozenset[str] = field(default_factory=frozenset)
    default_decision: str = "allow"
    preview: OperationPreview | None = None

    def __post_init__(self):
        if not isinstance(self.id, str) or "." not in self.id:
            raise ValueError("operation id must be a namespaced string")
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("operation label must be non-empty text")
        if self.default_decision not in DECISIONS:
            raise ValueError(f"unsupported permission decision: {self.default_decision}")
        if not isinstance(self.effects, frozenset) or any(
            not isinstance(effect, str) or not effect for effect in self.effects
        ):
            raise TypeError("operation effects must be a frozenset of strings")
        if self.preview is not None and not isinstance(
            self.preview,
            OperationPreview,
        ):
            raise TypeError("operation preview must be an OperationPreview")


@dataclass(frozen=True)
class InvocationContext:
    """Trusted origin assigned by the runtime at an invocation boundary."""

    origin: str
    explicit: bool = False

    def __post_init__(self):
        if self.origin not in ORIGINS:
            raise ValueError(f"unsupported invocation origin: {self.origin}")
        if not isinstance(self.explicit, bool):
            raise TypeError("explicit must be boolean")


@dataclass(frozen=True)
class PermissionRequest:
    request_id: str
    operation: OperationDefinition
    arguments: dict
    context: InvocationContext


class PermissionDenied(PermissionError):
    """Raised before an operation executes when authorization fails."""

    def __init__(self, operation_id: str):
        super().__init__(f"permission denied for '{operation_id}'")
        self.operation_id = operation_id


class PermissionBroker:
    """Apply exact-operation policy at the point an operation executes.

    This broker governs operations that voluntarily pass through it. It is not
    a sandbox and cannot constrain arbitrary Python code in a module.
    """

    def __init__(
        self,
        ask: Callable[[PermissionRequest], bool] | None = None,
        rules: Mapping[str, str] | None = None,
    ):
        self._ask = ask
        self._rules = dict(rules or {})
        for operation_id, decision in self._rules.items():
            if not isinstance(operation_id, str) or "." not in operation_id:
                raise ValueError("permission rule ids must be namespaced strings")
            if decision not in DECISIONS:
                raise ValueError(f"unsupported permission decision: {decision}")
        self._sequence = itertools.count(1)
        self._lock = threading.Lock()

    def decision_for(self, operation: OperationDefinition) -> str:
        return self._rules.get(operation.id, operation.default_decision)

    def authorize(
        self,
        operation: OperationDefinition,
        arguments: dict,
        context: InvocationContext,
    ) -> bool:
        if not isinstance(operation, OperationDefinition):
            raise TypeError("operation must be an OperationDefinition")
        if not isinstance(arguments, dict):
            raise TypeError("operation arguments must be a mapping")
        if not isinstance(context, InvocationContext):
            raise TypeError("context must be an InvocationContext")

        decision = self.decision_for(operation)
        if decision == "allow":
            return True
        if decision == "deny" or self._ask is None:
            return False
        with self._lock:
            request_id = f"permission-{next(self._sequence)}"
        request = PermissionRequest(
            request_id=request_id,
            operation=operation,
            arguments=deepcopy(arguments),
            context=context,
        )
        return bool(self._ask(request))

    def require(
        self,
        operation: OperationDefinition,
        arguments: dict,
        context: InvocationContext,
    ) -> None:
        if not self.authorize(operation, arguments, context):
            raise PermissionDenied(operation.id)
