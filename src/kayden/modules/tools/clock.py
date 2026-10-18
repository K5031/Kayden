from datetime import datetime

from kayden.interfaces import ToolInterface


class Module(ToolInterface):
    def schemas(self) -> list[dict]:
        return [{
            "type": "function",
            "function": {
                "name": "current_time",
                "description": "Get the current local date, time, and UTC offset.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }]

    def describe_activity(self, name, arguments, state, result=None):
        if state == "running":
            return "Checking the time"
        if state == "failed":
            return "Couldn’t check the time"
        return "Checked the time"

    def execute(self, name: str, arguments: dict) -> str:
        if name != "current_time":
            raise ValueError(f"Unknown tool: {name}")
        if arguments:
            raise ValueError("current_time takes no arguments")
        return datetime.now().astimezone().isoformat(timespec="seconds")
