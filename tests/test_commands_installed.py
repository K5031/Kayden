"""The commands an install puts on the PATH."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_kd_is_a_short_name_for_kayden():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    scripts = project["project"]["scripts"]
    assert scripts["kd"] == scripts["kayden"] == "kayden.__main__:main"


def test_the_bundle_starts_the_terminal_app_as_kayden_or_kd():
    import kayden.__main__
    import kayden.window
    from kayden_desktop.commands import launcher_for

    assert launcher_for("kayden") is kayden.__main__.main
    assert launcher_for("kd") is kayden.__main__.main
    assert launcher_for("kayden-desktop") is kayden.window.main
