#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec python3 -m dedup web --open "$@"
