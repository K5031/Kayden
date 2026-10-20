import difflib
import os
from pathlib import Path
import shutil

from kayden.interfaces import ToolInterface
from kayden.permissions import OperationDefinition, OperationPreview


DEFAULT_MAX_FILE_BYTES = 256 * 1024
MAX_RESULTS = 100
MAX_PREVIEW_CHARACTERS = 24 * 1024


class Module(ToolInterface):
    """Read and modify files beneath explicitly allowed filesystem roots."""

    def __init__(
        self,
        root=None,
        roots=None,
        allow_hidden=False,
        max_file_bytes=DEFAULT_MAX_FILE_BYTES,
    ):
        if root is not None and roots is not None:
            raise ValueError("configure filesystem root or roots, not both")
        if not isinstance(allow_hidden, bool):
            raise TypeError("filesystem allow_hidden must be boolean")
        if (
            not isinstance(max_file_bytes, int)
            or isinstance(max_file_bytes, bool)
            or max_file_bytes < 1
        ):
            raise TypeError("filesystem max_file_bytes must be a positive integer")

        configured = self._configured_roots(root, roots)
        resolved = []
        for value in configured:
            path = Path(value).expanduser().resolve()
            if not path.is_dir():
                raise ValueError(f"filesystem root is not a directory: {path}")
            if path not in resolved:
                resolved.append(path)
        if not resolved:
            raise ValueError("filesystem requires at least one root")

        self.roots = tuple(resolved)
        self.root = self.roots[0]
        self.allow_hidden = allow_hidden
        self.max_file_bytes = max_file_bytes

    @staticmethod
    def _configured_roots(root, roots):
        if root is not None:
            return [root]
        if roots is not None:
            if not isinstance(roots, (list, tuple)) or not roots:
                raise TypeError("filesystem roots must be a non-empty list")
            if any(not isinstance(value, (str, os.PathLike)) for value in roots):
                raise TypeError("filesystem roots must contain paths")
            return list(roots)

        environment = os.environ.get("KAYDEN_FILESYSTEM_ROOTS")
        if environment:
            return [value for value in environment.split(os.pathsep) if value]
        legacy = os.environ.get("KAYDEN_FILESYSTEM_ROOT")
        if legacy:
            return [legacy]
        return [os.getcwd(), Path.home()]

    def schemas(self) -> list[dict]:
        path_property = {
            "type": "string",
            "description": (
                "An absolute path beneath an allowed root, or a path relative "
                "to the primary root. Use list_filesystem_roots to inspect them."
            ),
        }
        return [
            self._schema(
                "list_filesystem_roots",
                "Show the directories this filesystem module can access.",
                {},
            ),
            self._schema(
                "list_directory",
                "List the visible contents of a directory.",
                {"path": path_property},
            ),
            self._schema(
                "read_text_file",
                "Read a UTF-8 text file within the configured size limit.",
                {"path": path_property},
                ["path"],
            ),
            self._schema(
                "search_files",
                "Recursively find visible files by filename.",
                {
                    "query": {
                        "type": "string",
                        "description": "Case-insensitive text in the filename.",
                    },
                    "path": path_property,
                },
                ["query"],
            ),
            self._schema(
                "search_text_files",
                "Recursively search visible UTF-8 text files.",
                {
                    "query": {
                        "type": "string",
                        "description": "Case-sensitive text to find.",
                    },
                    "path": path_property,
                },
                ["query"],
            ),
            self._schema(
                "write_text_file",
                "Create or replace a UTF-8 text file.",
                {
                    "path": path_property,
                    "content": {"type": "string"},
                    "create_parents": {"type": "boolean"},
                },
                ["path", "content"],
            ),
            self._schema(
                "replace_text_in_file",
                "Replace exact text in an existing UTF-8 file.",
                {
                    "path": path_property,
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                ["path", "old", "new"],
            ),
            self._schema(
                "create_directory",
                "Create a directory and any missing parents.",
                {"path": path_property},
                ["path"],
            ),
            self._schema(
                "move_path",
                "Move or rename a file or directory to a new path.",
                {"source": path_property, "destination": path_property},
                ["source", "destination"],
            ),
            self._schema(
                "delete_path",
                "Delete a file or directory. Recursive directory deletion is optional.",
                {"path": path_property, "recursive": {"type": "boolean"}},
                ["path"],
            ),
        ]

    @staticmethod
    def _schema(name, description, properties, required=None):
        parameters = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            parameters["required"] = required
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        }

    def operation(self, name, arguments):
        metadata = {
            "list_filesystem_roots": (
                "Inspect filesystem roots",
                "filesystem.read",
            ),
            "list_directory": ("List a directory", "filesystem.read"),
            "read_text_file": ("Read a text file", "filesystem.read"),
            "search_files": ("Search filenames", "filesystem.read"),
            "search_text_files": ("Search text files", "filesystem.read"),
            "write_text_file": ("Write a text file", "filesystem.write"),
            "replace_text_in_file": ("Modify a text file", "filesystem.write"),
            "create_directory": ("Create a directory", "filesystem.write"),
            "move_path": ("Move a filesystem path", "filesystem.move"),
            "delete_path": ("Delete a filesystem path", "filesystem.delete"),
        }
        if name not in metadata:
            return super().operation(name, arguments)
        label, effect = metadata[name]
        decision = (
            "ask"
            if name in {
                "write_text_file",
                "replace_text_in_file",
                "create_directory",
                "move_path",
                "delete_path",
            }
            else "allow"
        )
        return OperationDefinition(
            id=f"tool.{name}",
            label=label,
            effects=frozenset({effect}),
            default_decision=decision,
            preview=self._operation_preview(name, arguments),
        )

    def _operation_preview(self, name, arguments):
        if name == "write_text_file":
            path = self._resolve_mutation(arguments.get("path"))
            content = arguments.get("content")
            if not isinstance(content, str):
                return None
            before = self._read(path) if path.exists() and path.is_file() else ""
            return self._diff_preview(path, before, content)

        if name == "replace_text_in_file":
            path = self._resolve_mutation(arguments.get("path"))
            old = arguments.get("old")
            new = arguments.get("new")
            if not isinstance(old, str) or not old or not isinstance(new, str):
                return None
            before = self._read(path)
            replace_all = arguments.get("replace_all", False)
            if not isinstance(replace_all, bool):
                return None
            after = before.replace(old, new, -1 if replace_all else 1)
            return self._diff_preview(path, before, after)

        if name == "delete_path":
            path = self._resolve_mutation(arguments.get("path"))
            if path.is_file() and not path.is_symlink():
                try:
                    return self._diff_preview(path, self._read(path), "")
                except ValueError:
                    pass
            recursive = arguments.get("recursive", False)
            suffix = " recursively" if recursive else ""
            return OperationPreview(
                kind="text",
                title="Proposed deletion",
                content=f"Delete {path}{suffix}",
            )

        if name == "move_path":
            source = self._resolve_mutation(arguments.get("source"))
            destination = self._resolve_mutation(arguments.get("destination"))
            return OperationPreview(
                kind="text",
                title="Proposed move",
                content=f"{source}\n→ {destination}",
            )

        if name == "create_directory":
            path = self._resolve_mutation(arguments.get("path"))
            return OperationPreview(
                kind="text",
                title="Proposed directory",
                content=f"Create {path}/",
            )
        return None

    def _diff_preview(self, path, before, after):
        if before == after:
            return OperationPreview(
                kind="text",
                title="Proposed changes",
                content="No textual changes.",
            )
        diff = "\n".join(difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        ))
        if len(diff) > MAX_PREVIEW_CHARACTERS:
            diff = diff[:MAX_PREVIEW_CHARACTERS] + "\n[diff truncated]"
        return OperationPreview(
            kind="diff",
            title="Proposed changes",
            content=diff,
        )

    def describe_activity(self, name, arguments, state, result=None):
        path = " ".join(str(arguments.get("path", ".")).split())[:72]
        query = " ".join(str(arguments.get("query", "")).split())[:72]
        source = " ".join(str(arguments.get("source", "")).split())[:72]
        destination = " ".join(
            str(arguments.get("destination", "")).split()
        )[:72]
        descriptions = {
            "list_filesystem_roots": (
                "Checking accessible folders",
                "Checked accessible folders",
                "Couldn’t check accessible folders",
            ),
            "list_directory": (
                f"Browsing “{path}”",
                f"Browsed “{path}”",
                f"Couldn’t browse “{path}”",
            ),
            "read_text_file": (
                f"Reading “{path}”",
                f"Read “{path}”",
                f"Couldn’t read “{path}”",
            ),
            "search_files": (
                f"Searching filenames for “{query}”",
                f"Searched filenames for “{query}”",
                "Filename search failed",
            ),
            "search_text_files": (
                f"Searching files for “{query}”",
                f"Searched files for “{query}”",
                "Text search failed",
            ),
            "write_text_file": (
                f"Writing “{path}”",
                f"Wrote “{path}”",
                f"Couldn’t write “{path}”",
            ),
            "replace_text_in_file": (
                f"Editing “{path}”",
                f"Edited “{path}”",
                f"Couldn’t edit “{path}”",
            ),
            "create_directory": (
                f"Creating “{path}”",
                f"Created “{path}”",
                f"Couldn’t create “{path}”",
            ),
            "move_path": (
                f"Moving “{source}” to “{destination}”",
                f"Moved “{source}” to “{destination}”",
                f"Couldn’t move “{source}”",
            ),
            "delete_path": (
                f"Deleting “{path}”",
                f"Deleted “{path}”",
                f"Couldn’t delete “{path}”",
            ),
        }
        running, finished, failed = descriptions[name]
        return {"running": running, "finished": finished, "failed": failed}[state]

    def _resolve(self, configured_path: str) -> Path:
        if not isinstance(configured_path, str) or not configured_path:
            raise ValueError("path must be a non-empty string")
        raw_path = Path(configured_path).expanduser()
        if not self.allow_hidden and any(
            part.startswith(".") and part not in {".", ".."}
            for part in raw_path.parts
        ):
            raise ValueError("hidden paths are not accessible")
        candidate = (
            raw_path if raw_path.is_absolute() else self.root / raw_path
        ).resolve(strict=False)
        if not any(self._is_within(candidate, root) for root in self.roots):
            raise ValueError("path is outside the allowed filesystem roots")
        return candidate

    def _resolve_mutation(self, configured_path: str) -> Path:
        """Resolve parent links while preserving the final directory entry."""
        if not isinstance(configured_path, str) or not configured_path:
            raise ValueError("path must be a non-empty string")
        raw_path = Path(configured_path).expanduser()
        if not self.allow_hidden and any(
            part.startswith(".") and part not in {".", ".."}
            for part in raw_path.parts
        ):
            raise ValueError("hidden paths are not accessible")
        unresolved = raw_path if raw_path.is_absolute() else self.root / raw_path
        candidate = unresolved.parent.resolve(strict=False) / unresolved.name
        if not any(self._is_within(candidate, root) for root in self.roots):
            raise ValueError("path is outside the allowed filesystem roots")
        return candidate

    @staticmethod
    def _is_within(path, root):
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _display(self, path):
        if self._is_within(path, self.root):
            relative = path.relative_to(self.root)
            return "." if not relative.parts else str(relative)
        return str(path)

    def _visible(self, path):
        if self.allow_hidden:
            return True
        for root in self.roots:
            if self._is_within(path, root):
                return not any(
                    part.startswith(".") for part in path.relative_to(root).parts
                )
        return False

    @staticmethod
    def _validate_arguments(arguments: dict, allowed: set[str]) -> None:
        if not set(arguments).issubset(allowed):
            raise ValueError("unexpected arguments")

    @staticmethod
    def _boolean(arguments, name, default=False):
        value = arguments.get(name, default)
        if not isinstance(value, bool):
            raise TypeError(f"{name} must be boolean")
        return value

    def _read(self, path):
        if not path.is_file():
            raise ValueError("path is not a file")
        if path.stat().st_size > self.max_file_bytes:
            raise ValueError(f"file is larger than {self.max_file_bytes} bytes")
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("file is not UTF-8 text") from error

    def execute(self, name: str, arguments: dict) -> str:
        if name == "list_filesystem_roots":
            self._validate_arguments(arguments, set())
            return "\n".join(str(root) for root in self.roots)

        if name == "list_directory":
            self._validate_arguments(arguments, {"path"})
            directory = self._resolve(arguments.get("path", "."))
            if not directory.is_dir():
                raise ValueError("path is not a directory")
            entries = []
            for entry in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
                if not self._visible(entry):
                    continue
                suffix = "/" if entry.is_dir() else ""
                entries.append(f"{self._display(entry)}{suffix}")
                if len(entries) == MAX_RESULTS:
                    entries.append("[results truncated]")
                    break
            return "\n".join(entries) or "[empty directory]"

        if name == "read_text_file":
            self._validate_arguments(arguments, {"path"})
            if "path" not in arguments:
                raise ValueError("read_text_file requires a path")
            return self._read(self._resolve(arguments["path"]))

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
                if not self._visible(entry):
                    continue
                if entry.is_file() and query.lower() in entry.name.lower():
                    matches.append(self._display(entry))
                    if len(matches) == MAX_RESULTS:
                        matches.append("[results truncated]")
                        break
            return "\n".join(sorted(matches)) or "[no matching files]"

        if name == "search_text_files":
            self._validate_arguments(arguments, {"query", "path"})
            query = arguments.get("query")
            if not isinstance(query, str) or not query:
                raise ValueError("search_text_files requires a non-empty query")
            directory = self._resolve(arguments.get("path", "."))
            if not directory.is_dir():
                raise ValueError("path is not a directory")
            matches = []
            for entry in directory.rglob("*"):
                if not entry.is_file() or not self._visible(entry):
                    continue
                try:
                    if entry.stat().st_size > self.max_file_bytes:
                        continue
                    lines = entry.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeDecodeError):
                    continue
                for line_number, line in enumerate(lines, 1):
                    if query in line:
                        matches.append(
                            f"{self._display(entry)}:{line_number}: {line[:240]}"
                        )
                        if len(matches) == MAX_RESULTS:
                            matches.append("[results truncated]")
                            return "\n".join(matches)
            return "\n".join(matches) or "[no matching text]"

        if name == "write_text_file":
            self._validate_arguments(
                arguments,
                {"path", "content", "create_parents"},
            )
            path = self._resolve_mutation(arguments.get("path"))
            content = arguments.get("content")
            if not isinstance(content, str):
                raise TypeError("content must be text")
            if len(content.encode("utf-8")) > self.max_file_bytes:
                raise ValueError("content exceeds the filesystem size limit")
            if path.exists() and not path.is_file():
                raise ValueError("path is not a file")
            if path.is_symlink():
                raise ValueError("symbolic links cannot be overwritten")
            if self._boolean(arguments, "create_parents"):
                path.parent.mkdir(parents=True, exist_ok=True)
            elif not path.parent.is_dir():
                raise ValueError("parent directory does not exist")
            path.write_text(content, encoding="utf-8")
            return f"Wrote {self._display(path)}."

        if name == "replace_text_in_file":
            self._validate_arguments(
                arguments,
                {"path", "old", "new", "replace_all"},
            )
            path = self._resolve_mutation(arguments.get("path"))
            old = arguments.get("old")
            new = arguments.get("new")
            if not isinstance(old, str) or not old:
                raise ValueError("old must be non-empty text")
            if not isinstance(new, str):
                raise TypeError("new must be text")
            if path.is_symlink():
                raise ValueError("symbolic links cannot be modified")
            content = self._read(path)
            occurrences = content.count(old)
            if occurrences == 0:
                raise ValueError("old text was not found")
            replace_all = self._boolean(arguments, "replace_all")
            updated = content.replace(old, new, -1 if replace_all else 1)
            if len(updated.encode("utf-8")) > self.max_file_bytes:
                raise ValueError("updated content exceeds the filesystem size limit")
            path.write_text(updated, encoding="utf-8")
            count = occurrences if replace_all else 1
            return f"Replaced {count} occurrence{'s' if count != 1 else ''}."

        if name == "create_directory":
            self._validate_arguments(arguments, {"path"})
            path = self._resolve_mutation(arguments.get("path"))
            path.mkdir(parents=True, exist_ok=True)
            return f"Created {self._display(path)}/."

        if name == "move_path":
            self._validate_arguments(arguments, {"source", "destination"})
            source = self._resolve_mutation(arguments.get("source"))
            destination = self._resolve_mutation(arguments.get("destination"))
            if source in self.roots:
                raise ValueError("cannot move an allowed filesystem root")
            if not source.exists() and not source.is_symlink():
                raise ValueError("source does not exist")
            if destination.exists() or destination.is_symlink():
                raise ValueError("destination already exists")
            if not destination.parent.is_dir():
                raise ValueError("destination parent does not exist")
            shutil.move(str(source), str(destination))
            return f"Moved {self._display(source)} to {self._display(destination)}."

        if name == "delete_path":
            self._validate_arguments(arguments, {"path", "recursive"})
            path = self._resolve_mutation(arguments.get("path"))
            if path in self.roots:
                raise ValueError("cannot delete an allowed filesystem root")
            if not path.exists() and not path.is_symlink():
                raise ValueError("path does not exist")
            recursive = self._boolean(arguments, "recursive")
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                if recursive:
                    shutil.rmtree(path)
                else:
                    path.rmdir()
            else:
                path.unlink()
            return f"Deleted {self._display(path)}."

        raise ValueError(f"Unknown tool: {name}")
