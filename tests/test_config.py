import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ConfigTests(unittest.TestCase):
    def test_import_has_no_filesystem_side_effect_and_load_creates_defaults(self):
        source_dir = str(Path(__file__).resolve().parents[1] / "src")
        with tempfile.TemporaryDirectory() as home:
            code = """
from pathlib import Path
import config

config_dir = Path.home() / '.config' / 'k'
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
