#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
exec "${NEO_PYTHON:-python3}" -B -m updater.update "$@"
