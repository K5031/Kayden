"""Briefcase app module: the bundle runs `python -m kadence_desktop`.

The package's one program starts the window launcher, or, run as `kadence` or `kd` (links
the package adds), the terminal launcher."""

import sys
from pathlib import Path

from kadence_desktop.commands import launcher_for

launcher_for(Path(sys.argv[0]).name)()
