#!/bin/bash
set -euo pipefail

if [ "$PWD" = "/" ]; then
  echo "Error: set a WORKDIR in Dockerfile." >&2
  exit 1
fi

python3 -m pytest "${TEST_DIR:-/tests}/test_outputs.py" -rA -v
