#!/bin/sh
set -eu
[ "$(uname -s)" = Linux ] || { echo 'This installer is Linux-only; no changes made.' >&2; exit 2; }
[ "$(uname -m)" = x86_64 ] || { echo 'This reviewed lock targets x86_64 Linux. Other architectures need a separate lock/acceptance.' >&2; exit 2; }
umask 077
ROOT=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
PYTHON=${PYTHON:-python3}
command -v "$PYTHON" >/dev/null 2>&1 || { echo 'Install Python3.11/3.12 and its venv package first. See README.' >&2; exit 2; }
TAG=$("$PYTHON" -I -B "$ROOT/packaging/linux_env.py" key)
LOCK="$ROOT/.install-linux.lock"
mkdir "$LOCK" 2>/dev/null || { echo 'Another installation owns .install-linux.lock. If interrupted, verify it stopped before removing that empty lock directory.' >&2; exit 2; }
trap 'rmdir "$LOCK" 2>/dev/null || :' 0
trap 'exit 130' INT TERM
VENV="$ROOT/.venv-linux/$TAG"
if [ -e "$VENV" ] && [ ! -f "$VENV/.friday-owned" ]; then
    echo 'Refusing to reuse an unrecognized environment. Choose a clean checkout.' >&2; exit 2
fi
if [ ! -d "$VENV" ]; then
    mkdir -p "$VENV"
    printf 'FRIDAY isolated Linux environment\n' > "$VENV/.friday-owned"
    "$PYTHON" -m venv "$VENV" || { echo 'venv creation failed. On Debian/Ubuntu install python3-venv (or the matching version package), then retry.' >&2; exit 1; }
fi
[ -x "$VENV/bin/python" ] || { echo 'The owned environment is incomplete. Remove only its .venv-linux subdirectory and retry.' >&2; exit 1; }
# Package installation occurs only inside this environment; never sudo pip.
# A failed rerun must not leave an old success marker on a partially changed environment.
rm -f "$VENV/.installed"
(
    unset PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_FIND_LINKS PIP_TRUSTED_HOST PIP_PROXY PIP_TARGET PIP_PREFIX PIP_USER
    export PIP_CONFIG_FILE=/dev/null
    "$VENV/bin/python" -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: --index-url https://pypi.org/simple -r "$ROOT/requirements-linux.lock"
)
"$VENV/bin/python" -m pip check
"$VENV/bin/python" -I -B "$ROOT/packaging/tokenizer_cache.py" install
LITELLM_LOCAL_MODEL_COST_MAP=True SCARF_NO_ANALYTICS=true "$VENV/bin/python" -I -B "$ROOT/software/linux_main.py" check
printf '%s\n' "$TAG" > "$VENV/.installed.tmp"
mv "$VENV/.installed.tmp" "$VENV/.installed"
printf '\nInstalled isolated Linux text mode. Next: sh ./run-linux.sh setup\n'