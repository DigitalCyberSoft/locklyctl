#!/usr/bin/env bash
# Open the locklyctl desktop app. Sets up the virtualenv and the GUI packages
# on first run (via run.sh), then launches the window.
#
#   ./gui.sh            # open the app
#   ./gui.sh --debug    # also print protocol-level logging to the terminal

set -euo pipefail
exec "$(dirname "$0")/run.sh" gui "$@"
