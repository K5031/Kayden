"""Optional functionality modules can contribute without cross-importing peers."""

from dataclasses import dataclass, field
from typing import Callable


CONTROL_KINDS = {"button", "toggle", "selection"}
CONTROL_PLACEMENTS = {"header", "composer", "menu"}


@dataclass(frozen=True)
class ControlContribution:
    """A generic user control owned and handled by its contributing module."""

    id: str
    label: str
    kind: str
    invoke: Callable[[object], object] = field(repr=False, compare=False)
    value: object = None
    enabled: bool = True
    icon: str | None = None
    placement: str = "header"
    options: tuple[dict, ...] = ()

    def __post_init__(self):
        if not isinstance(self.id, str) or "." not in self.id:
            raise ValueError("control contribution id must be a namespaced string")
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("control contribution label must be non-empty text")
        if self.kind not in CONTROL_KINDS:
            raise ValueError(f"unsupported control kind: {self.kind}")
        if self.placement not in CONTROL_PLACEMENTS:
            raise ValueError(f"unsupported control placement: {self.placement}")
        if not callable(self.invoke):
            raise TypeError("control contribution handler must be callable")
        if self.kind == "toggle" and not isinstance(self.value, bool):
            raise TypeError("toggle contribution value must be boolean")

    def descriptor(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "value": self.value,
            "enabled": self.enabled,
            "icon": self.icon,
            "placement": self.placement,
            "options": [dict(option) for option in self.options],
        }
