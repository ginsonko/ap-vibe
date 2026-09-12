#!/bin/sh
set -eu
APV_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APV_PYTHON=${AP_VIBE_PYTHON:-python3}
"$APV_PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), "AP-Vibe requires Python 3.11 or newer"'
exec "$APV_PYTHON" "$APV_ROOT/tools/posix_lifecycle.py" "$@"
