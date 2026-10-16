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

        self.assertIn('kayden = "main:main"', project_text)
        self.assertNotIn('\nk = "main:main"', project_text)

    def test_import_has_no_filesystem_side_effect_and_load_creates_defaults(self):
        source_dir = str(Path(__file__).resolve().parents[1] / "src")
        with tempfile.TemporaryDirectory() as home:
            code = """
from pathlib import Path
import config

config_dir = Path.home() / '.config' / 'kayden'
assert not config_dir.exists()
values = config.load_config()
assert values['controller'] == 'cloud'
assert values['memory'] == 'none'
assert values['action'] == 'native'
assert values['tools'] == ['clock']
assert (config_dir / 'config.toml').is_file()
assert (config_dir / 'system_prompt.txt').is_file()
assert config.load_system_prompt().startswith('You are K')
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

    def test_legacy_config_directory_is_migrated(self):
        source_dir = str(Path(__file__).resolve().parents[1] / "src")
        with tempfile.TemporaryDirectory() as home:
            legacy_dir = Path(home) / ".config" / "k"
            legacy_dir.mkdir(parents=True)
            (legacy_dir / "config.toml").write_text('controller = "legacy"\n')
            (legacy_dir / "system_prompt.txt").write_text("custom prompt")
            code = """
from pathlib import Path
import config

legacy_dir = Path.home() / '.config' / 'k'
config_dir = Path.home() / '.config' / 'kayden'
assert config.load_config()['controller'] == 'legacy'
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


if __name__ == "__main__":
    unittest.main()
