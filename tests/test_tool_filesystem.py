from pathlib import Path
import tempfile
import unittest

from kayden.modules.tools.filesystem import Module as FilesystemTool
from kayden.permissions import InvocationContext, PermissionBroker
from kayden.runtime.tools import ToolRuntime


class FilesystemToolTests(unittest.TestCase):
    def test_lists_reads_and_searches_within_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "docs" / "Guide.txt").write_text("hello")
            (root / ".secret").write_text("hidden")
            filesystem = FilesystemTool(root=root)
            self.assertEqual(filesystem.execute("list_directory", {}), "docs/")
            self.assertEqual(filesystem.execute("read_text_file", {"path": "docs/Guide.txt"}), "hello")
            self.assertEqual(filesystem.execute("search_files", {"query": "guide"}), "docs/Guide.txt")
            self.assertEqual(
                filesystem.execute("search_text_files", {"query": "hello"}),
                "docs/Guide.txt:1: hello",
            )

    def test_rejects_traversal_and_hidden_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            filesystem = FilesystemTool(root=root)
            with self.assertRaisesRegex(ValueError, "outside"):
                filesystem.execute("read_text_file", {"path": "../outside.txt"})
            with self.assertRaisesRegex(ValueError, "hidden"):
                filesystem.execute("read_text_file", {"path": ".env"})

    def test_accepts_absolute_paths_beneath_multiple_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first = base / "first"
            second = base / "second"
            first.mkdir()
            second.mkdir()
            target = second / "outside-primary.txt"
            target.write_text("reachable")
            filesystem = FilesystemTool(roots=[first, second])

            self.assertEqual(
                filesystem.execute("read_text_file", {"path": str(target)}),
                "reachable",
            )
            self.assertEqual(
                filesystem.execute("list_filesystem_roots", {}),
                f"{first}\n{second}",
            )

    def test_creates_edits_moves_and_deletes_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            filesystem = FilesystemTool(root=root)

            filesystem.execute("create_directory", {"path": "work"})
            filesystem.execute("write_text_file", {
                "path": "work/note.txt",
                "content": "one one",
            })
            filesystem.execute("replace_text_in_file", {
                "path": "work/note.txt",
                "old": "one",
                "new": "two",
                "replace_all": True,
            })
            filesystem.execute("move_path", {
                "source": "work/note.txt",
                "destination": "work/moved.txt",
            })

            self.assertEqual((root / "work/moved.txt").read_text(), "two two")
            filesystem.execute("delete_path", {
                "path": "work",
                "recursive": True,
            })
            self.assertFalse((root / "work").exists())

    def test_every_filesystem_mutation_asks_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "existing.txt").write_text("original")
            filesystem = FilesystemTool(root=root)
            runtime = ToolRuntime([filesystem])
            prompts = []
            broker = PermissionBroker(
                ask=lambda request: prompts.append(request) or False
            )
            runtime.bind_authorizer(lambda operation, arguments: broker.require(
                operation,
                arguments,
                InvocationContext("model"),
            ))

            created_file = runtime.execute("write_text_file", {
                "path": "note.txt",
                "content": "new",
            })
            created_directory = runtime.execute(
                "create_directory",
                {"path": "folder"},
            )
            overwritten = runtime.execute("write_text_file", {
                "path": "existing.txt",
                "content": "changed",
            })
            deleted = runtime.execute(
                "delete_path",
                {"path": "existing.txt"},
            )

            self.assertTrue(created_file.is_error)
            self.assertTrue(created_directory.is_error)
            self.assertTrue(overwritten.is_error)
            self.assertTrue(deleted.is_error)
            self.assertFalse((root / "note.txt").exists())
            self.assertFalse((root / "folder").exists())
            self.assertEqual((root / "existing.txt").read_text(), "original")
            self.assertEqual(
                [request.operation.id for request in prompts],
                [
                    "tool.write_text_file",
                    "tool.create_directory",
                    "tool.write_text_file",
                    "tool.delete_path",
                ],
            )

    def test_hidden_access_can_be_enabled_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("TOKEN=test")
            filesystem = FilesystemTool(root=root, allow_hidden=True)

            self.assertEqual(
                filesystem.execute("read_text_file", {"path": ".env"}),
                "TOKEN=test",
            )

    def test_text_edit_permission_contains_unified_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "test.txt"
            path.write_text("pending")
            filesystem = FilesystemTool(root=root)

            operation = filesystem.operation("replace_text_in_file", {
                "path": str(path),
                "old": "pending",
                "new": "successful",
                "replace_all": False,
            })

            self.assertIsNotNone(operation.preview)
            self.assertEqual(operation.preview.kind, "diff")
            self.assertIn("-pending", operation.preview.content)
            self.assertIn("+successful", operation.preview.content)

    def test_deleting_a_symlink_does_not_delete_its_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.txt"
            link = root / "link.txt"
            target.write_text("keep")
            link.symlink_to(target)
            filesystem = FilesystemTool(root=root)

            filesystem.execute("delete_path", {"path": "link.txt"})

            self.assertFalse(link.exists())
            self.assertEqual(target.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
