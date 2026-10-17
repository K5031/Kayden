import os
from pathlib import Path

from interfaces import ToolInterface

MAX_FILE_BYTES = 64 * 1024
MAX_RESULTS = 100


class Module(ToolInterface):
    """Provide read-only access beneath one explicitly bounded directory."""

    def __init__(self, root=None):
        configured_root = root or os.environ.get("KAYDEN_FILESYSTEM_ROOT") or os.getcwd()
        self.root = Path(configured_root).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"filesystem root is not a directory: {self.root}")

    def schemas(self) -> list[dict]:
        path_property = {
            "type": "string",
            "description": "A relative path beneath the configured filesystem root.",
        }
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_directory",
                    "description": "List the visible contents of a directory.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": path_property},
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_text_file",
                    "description": "Read a UTF-8 text file up to 64 KiB.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": path_property},
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_files",
                    "description": "Recursively find visible files by filename.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Case-insensitive text in the filename.",
                            },
                            "path": path_property,
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    def _resolve(self, relative_path: str) -> Path:
        if not isinstance(relative_path, str) or not relative_path:
            raise ValueError("path must be a non-empty string")
        raw_path = Path(relative_path)
        if raw_path.is_absolute():
            raise ValueError("path must be relative to the filesystem root")
        if any(part.startswith(".") and part not in {".", ".."} for part in raw_path.parts):
            raise ValueError("hidden paths are not accessible")
        candidate = (self.root / raw_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as error:
            raise ValueError("path escapes the filesystem root") from error
        return candidate

    @staticmethod
    def _validate_arguments(arguments: dict, allowed: set[str]) -> None:
        if not set(arguments).issubset(allowed):
            raise ValueError("unexpected arguments")

    def execute(self, name: str, arguments: dict) -> str:
        if name == "list_directory":
            self._validate_arguments(arguments, {"path"})
            directory = self._resolve(arguments.get("path", "."))
            if not directory.is_dir():
                raise ValueError("path is not a directory")
            entries = []
            for entry in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
                if entry.name.startswith("."):
                    continue
                suffix = "/" if entry.is_dir() else ""
                entries.append(f"{entry.relative_to(self.root)}{suffix}")
                if len(entries) == MAX_RESULTS:
                    entries.append("[results truncated]")
                    break
            return "\n".join(entries) or "[empty directory]"

        if name == "read_text_file":
            self._validate_arguments(arguments, {"path"})
            if "path" not in arguments:
                raise ValueError("read_text_file requires a path")
            file_path = self._resolve(arguments["path"])
            if not file_path.is_file():
                raise ValueError("path is not a file")
            if file_path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("file is larger than 64 KiB")
            try:
                return file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("file is not UTF-8 text") from error

        if name == "search_files":
            self._validate_arguments(arguments, {"query", "path"})
            query = arguments.get("query")
            if not isinstance(query, str) or not query:
                raise ValueError("search_files requires a non-empty query")
            directory = self._resolve(arguments.get("path", "."))
            if not directory.is_dir():
                raise ValueError("path is not a directory")
            matches = []
            for entry in directory.rglob("*"):
                relative = entry.relative_to(self.root)
                if any(part.startswith(".") for part in relative.parts):
                    continue
                if entry.is_file() and query.lower() in entry.name.lower():
                    matches.append(str(relative))
                    if len(matches) == MAX_RESULTS:
                        matches.append("[results truncated]")
                        break
            return "\n".join(sorted(matches)) or "[no matching files]"

        raise ValueError(f"Unknown tool: {name}")
