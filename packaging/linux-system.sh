#!/bin/sh
# Builds the desktop app as a package for this Linux (an Arch .pkg.tar.zst here) into dist/.
# Briefcase makes one program, kayden-desktop; this adds kayden and kd as links to it, which
# start the terminal app (kayden_desktop/commands.py). Run from the project root.
set -eu
briefcase() { uvx --python /usr/bin/python3 briefcase "$@" linux system --no-input; }
rm -rf build/kayden-desktop
briefcase create
briefcase build
for bin in build/kayden-desktop/*/*/kayden-desktop-*/usr/bin; do
    ln -sf kayden-desktop "$bin/kayden"
    ln -sf kayden-desktop "$bin/kd"
done
briefcase package
