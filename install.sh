#!/usr/bin/env bash
# CompassCairn install step.
#
# The tool is standard-library only, so there is nothing to download and no
# system package to install. This script verifies the interpreter, verifies the
# module imports, and (when pip is available) installs the local console script
# offline. It installs nothing else, seeds nothing, and EXITS.
set -eu
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "install: '$PYTHON' not found - install Python 3.9 or newer and retry" >&2
    exit 1
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "install: Python 3.9 or newer is required, found: $("$PYTHON" -V 2>&1)" >&2
    exit 1
fi

# Cheap self-check: an import error must fail here, not at first use.
if ! "$PYTHON" -c 'import compasscairn'; then
    echo "install: compasscairn.py failed to import" >&2
    exit 1
fi

# Optional: install the `compasscairn` console script from the local tree only
# (--no-index keeps this fully offline). Failure here is not fatal: the tool is
# always runnable as `python3 compasscairn.py`.
if "$PYTHON" -m pip --version >/dev/null 2>&1; then
    if "$PYTHON" -m pip install --no-index --no-build-isolation --quiet -e . >/dev/null 2>&1; then
        echo "install: console script 'compasscairn' installed"
    else
        echo "install: note - console script not installed; run: $PYTHON compasscairn.py"
    fi
fi

echo "install: ready - try '$PYTHON compasscairn.py --help' or 'sh demo.sh'"
