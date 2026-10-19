import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ConfigTests(unittest.TestCase):
    def test_console_command_is_named_kayden(self):
        project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
        project_text = project_file.read_text()

        self.assertIn('name = "kayden"', project_text)
        self.assertIn('kayden = "kayden.cli:main"', project_text)
        self.assertNotIn("py-modules", project_text)

    def test_import_has_no_filesystem_side_effect_and_load_creates_defaults(self):
        source_dir = str(Path(__file__).resolve().parents[1] / "src")
        with tempfile.TemporaryDirectory() as home:
            code = """
from pathlib import Path
from kayden import config

config_dir = Path.home() / '.config' / 'kayden'
assert not config_dir.exists()
values = config.load_config()
assert 'controller' not in values
assert values['frontend'] == 'tui'
assert 'input' not in values
assert 'output' not in values
assert values['memory'] == 'continuity'
assert values['action'] == 'native'
assert values['tools'] == ['clock', 'calculator', 'filesystem', 'web_fetch', 'web_exa']
assert values['assistant']['name'] == 'Kayden'
assert (config_dir / 'config.toml').is_file()
assert (config_dir / 'system_prompt.txt').is_file()
assert config.load_system_prompt().startswith('You are Kayden')
"""
            env = os.environ.copy()
            env["HOME"] = home
            env.pop("XDG_CONFIG_HOME", None)
            env["PYTHONPATH"] = source_dir
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd="/tmp",
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_old_terminal_config_is_normalized_to_tui_frontend(self):
        from kayden.config import _normalize_legacy_frontend

        values = _normalize_legacy_frontend({
            "controller": "cloud",
            "input": "terminal",
            "output": ["terminal", "kokoro"],
            "core": "deepseek",
        })

        self.assertEqual(values, {
            "frontend": "tui",
            "output": ["kokoro"],
            "core": "deepseek",
        })

        already_migrated = _normalize_legacy_frontend({
            "controller": "cloud",
            "frontend": "gui",
            "input": "terminal",
            "output": "terminal",
        })
        self.assertEqual(already_migrated, {
            "frontend": "tauri",
            "settings": {
                "frontend": {"tauri": {"mode": "gui"}},
            },
        })

    def test_legacy_web_tool_is_split_into_fetch_and_search_modules(self):
        from kayden.config import _normalize_legacy_frontend

        values = _normalize_legacy_frontend({
            "tools": ["clock", "web"],
        })
        self.assertEqual(values["tools"], ["clock", "web_fetch", "web_exa"])

        searxng = _normalize_legacy_frontend({
            "tools": ["web", "web_searxng"],
        })
        self.assertEqual(searxng["tools"], ["web_fetch", "web_searxng"])

    def test_legacy_config_directory_is_migrated(self):
        source_dir = str(Path(__file__).resolve().parents[1] / "src")
        with tempfile.TemporaryDirectory() as home:
            legacy_dir = Path(home) / ".config" / "k"
            legacy_dir.mkdir(parents=True)
            (legacy_dir / "config.toml").write_text(
                'controller = "legacy"\nfrontend = "gui"\n'
            )
            (legacy_dir / "system_prompt.txt").write_text("custom prompt")
            code = """
from pathlib import Path
from kayden import config

legacy_dir = Path.home() / '.config' / 'k'
config_dir = Path.home() / '.config' / 'kayden'
values = config.load_config()
assert 'controller' not in values
assert values['frontend'] == 'tauri'
assert values['settings']['frontend']['tauri']['mode'] == 'gui'
assert config.load_system_prompt() == 'custom prompt'
assert not legacy_dir.exists()
assert config_dir.is_dir()
"""
            env = os.environ.copy()
            env["HOME"] = home
            env.pop("XDG_CONFIG_HOME", None)
            env["PYTHONPATH"] = source_dir
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd="/tmp",
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_module_selections_are_saved_without_rewriting_settings(self):
        from kayden import config

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                'frontend = "qt"\n'
                'core = "chatgpt"\n'
                'output = "kokoro"\n'
                'tools = [\n'
                '    "clock",\n'
                ']\n\n'
                '[assistant]\n'
                'name = "Ahri"\n\n'
                '[settings.core.openai]\n'
                'model = "gpt-6-luna"\n'
            )

            config.save_module_selections({
                "core": "openai",
                "output": None,
                "tools": ["clock", "calculator"],
            }, path=path)

            text = path.read_text()
            with path.open("rb") as config_file:
                values = config.tomllib.load(config_file)
            self.assertEqual(values["core"], "openai")
            self.assertNotIn("output", values)
            self.assertEqual(values["tools"], ["clock", "calculator"])
            self.assertEqual(values["assistant"]["name"], "Ahri")
            self.assertIn('[settings.core.openai]', text)
            self.assertIn('model = "gpt-6-luna"', text)

            config.save_module_selections({"tools": []}, path=path)
            with path.open("rb") as config_file:
                self.assertEqual(config.tomllib.load(config_file)["tools"], [])


if __name__ == "__main__":
    unittest.main()
