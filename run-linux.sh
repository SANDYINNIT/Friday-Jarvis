#!/bin/sh
set -eu
[ "$(uname -s)" = Linux ] || { echo 'This launcher is Linux-only.' >&2; exit 2; }
[ "$(uname -m)" = x86_64 ] || { echo 'This build targets x86_64 Linux.' >&2; exit 2; }
umask 077
ROOT=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
PYTHON=${PYTHON:-python3}
TAG=$("$PYTHON" -I -B "$ROOT/packaging/linux_env.py" key)
VENV="$ROOT/.venv-linux/$TAG"
[ -x "$VENV/bin/python" ] && [ -f "$VENV/.installed" ] && [ "$(cat "$VENV/.installed")" = "$TAG" ] || { echo 'No verified environment for this lock/Python version. Run sh ./install-linux.sh first.' >&2; exit 2; }
export PYTHONNOUSERSITE=1 LITELLM_LOCAL_MODEL_COST_MAP=True SCARF_NO_ANALYTICS=true HF_HUB_OFFLINE=1
case "${1:-chat}" in
    setup|configure|check|n8n) exec "$VENV/bin/python" -I -B "$ROOT/software/public_entry.py" "$@" ;;
    *) exec "$VENV/bin/python" -I -B "$ROOT/software/public_entry.py" linux "$@" ;;
esac