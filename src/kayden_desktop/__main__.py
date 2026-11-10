"""Briefcase app module: the bundle runs `python -m kayden_desktop`.

The package's one program starts the window launcher, or, run as `kayden` or `kd` (links
the package adds), the terminal launcher."""

import sys
from pathlib import Path

from kayden_desktop.commands import launcher_for

launcher_for(Path(sys.argv[0]).name)()
