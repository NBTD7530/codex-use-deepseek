#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec /usr/bin/python3 "$project_root/src/installer.py" install
