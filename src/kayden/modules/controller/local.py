from kayden.interfaces import ControllerInterface
from kayden.modules.controller._frontend import run_frontend_controller


class Module(ControllerInterface):
    required_modules = ["frontend", "core", "context", "memory", "action", "tools"]
    optional_modules = ["input", "output"]
    required_features = {
        "core": {"runtime_kind": "local"},
        "context": {"usage_mode": "self"},
    }

    def run(self, api) -> None:
        run_frontend_controller(api)
