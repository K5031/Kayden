"""The commands an install puts on the PATH."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_kd_is_a_short_name_for_kayden():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    scripts = project["project"]["scripts"]
    assert scripts["kd"] == scripts["kayden"] == "kayden.__main__:main"
