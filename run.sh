#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONDONTWRITEBYTECODE=1
exec "${NEO_PYTHON:-python3}" "$ROOT/studio.py" "$@"
