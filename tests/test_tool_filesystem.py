from pathlib import Path
import tempfile
import unittest

from kayden.modules.tools.filesystem import Module as FilesystemTool


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

    def test_rejects_traversal_and_hidden_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            filesystem = FilesystemTool(root=root)
            with self.assertRaisesRegex(ValueError, "escapes"):
                filesystem.execute("read_text_file", {"path": "../outside.txt"})
            with self.assertRaisesRegex(ValueError, "hidden"):
                filesystem.execute("read_text_file", {"path": ".env"})


if __name__ == "__main__":
    unittest.main()
