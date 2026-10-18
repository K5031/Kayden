import ast
from pathlib import Path
import unittest


MODULES = Path(__file__).resolve().parents[1] / "src/kayden/modules"


class ModuleLayoutTests(unittest.TestCase):
    def test_multifile_modules_have_an_explicit_main_entrypoint(self):
        module_directories = sorted(
            path
            for capability in MODULES.iterdir()
            if capability.is_dir() and not capability.name.startswith("__")
            for path in capability.iterdir()
            if path.is_dir()
            and not path.name.startswith("__")
            and (path / "__init__.py").is_file()
        )

        self.assertTrue(module_directories)
        for module in module_directories:
            with self.subTest(module=module.relative_to(MODULES)):
                main = module / "main.py"
                self.assertTrue(main.is_file(), f"{module} has no main.py")
                classes = {
                    node.name
                    for node in ast.parse(main.read_text()).body
                    if isinstance(node, ast.ClassDef)
                }
                self.assertIn("Module", classes)

                package = (module / "__init__.py").read_text()
                self.assertIn("from .main import Module", package)
                self.assertIn('"Module"', package)


if __name__ == "__main__":
    unittest.main()
