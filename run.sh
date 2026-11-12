#!/usr/bin/env bash
# locklyctl startup helper: create a venv, install deps once, then launch.
# Re-running is cheap — it skips setup if the venv is already built.
#
#   ./run.sh            # set up (first run) and launch the menu
#   ./run.sh gui        # set up with the desktop-app extra and open the app
#   ./run.sh --debug    # launch with protocol-level logging
#   LOCKLYCTL_RECREATE=1 ./run.sh   # rebuild the venv from scratch

set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
PY="${PYTHON:-python3}"

if [[ "${LOCKLYCTL_RECREATE:-}" == "1" ]]; then
  rm -rf "$VENV"
fi

if [[ ! -d "$VENV" ]]; then
  echo "· creating virtualenv in $VENV"
  "$PY" -m venv "$VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

# The GUI's extra is large (a web engine on Linux), so it's only installed
# when the app is asked for.
EXTRAS="keyring"
[[ "${1:-}" == "gui" ]] && EXTRAS="keyring,gui"
STAMP="$VENV/.installed-${EXTRAS/,/-}"

# Install (editable) only when this set of extras is missing or sources changed.
if ! command -v locklyctl >/dev/null 2>&1 || [[ ! -f "$STAMP" || pyproject.toml -nt "$STAMP" ]]; then
  echo "· installing locklyctl and dependencies ($EXTRAS)"
  pip install --quiet --upgrade pip
  pip install --quiet -e ".[$EXTRAS]"
  touch "$STAMP"
fi

exec locklyctl "$@"
