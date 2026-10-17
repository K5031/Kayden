from interfaces import ControllerInterface
from modules.controller._frontend import run_frontend_controller


class Module(ControllerInterface):
    required_modules = ["frontend", "core", "context", "memory", "action", "tools"]
    optional_modules = ["input", "output"]
    required_features = {
        "core": {"runtime_kind": "cloud", "reports_usage": True},
        "context": {"usage_mode": "reported"},
    }

    def run(self, api) -> None:
        run_frontend_controller(api, report_usage=True)
