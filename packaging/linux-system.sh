#!/bin/sh
# Builds the desktop app as a package for this Linux (an Arch .pkg.tar.zst here) into dist/.
# Briefcase makes one program, kadence-desktop; this adds kadence and kd as links to it, which
# start the terminal app (kadence_desktop/commands.py). Run from the project root.
set -eu
briefcase() { uvx --python /usr/bin/python3 briefcase "$@" linux system --no-input; }
rm -rf build/kadence-desktop
briefcase create
briefcase build
for bin in build/kadence-desktop/*/*/kadence-desktop-*/usr/bin; do
    ln -sf kadence-desktop "$bin/kadence"
    ln -sf kadence-desktop "$bin/kd"
done
briefcase package
