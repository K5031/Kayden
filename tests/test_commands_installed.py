"""The commands an install puts on the PATH."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_kd_is_a_short_name_for_kadence():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    scripts = project["project"]["scripts"]
    assert scripts["kd"] == scripts["kadence"] == "kadence.__main__:main"


def test_the_bundle_starts_the_terminal_app_as_kadence_or_kd():
    import kadence.__main__
    import kadence.window
    from kadence_desktop.commands import launcher_for

    assert launcher_for("kadence") is kadence.__main__.main
    assert launcher_for("kd") is kadence.__main__.main
    assert launcher_for("kadence-desktop") is kadence.window.main
