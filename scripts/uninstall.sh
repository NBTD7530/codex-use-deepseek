#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ "$#" -gt 0 ]; then
    exec /usr/bin/python3 "$project_root/src/installer.py" rollback "$1"
fi
exec /usr/bin/python3 "$project_root/src/installer.py" rollback
