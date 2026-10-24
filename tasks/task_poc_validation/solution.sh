#!/bin/bash
set -euo pipefail

cd /workspace

if [ -n "${GOLD_PATCHES_DIR:-}" ] && [ -d "${GOLD_PATCHES_DIR}" ]; then
  PATCH_ROOT="${GOLD_PATCHES_DIR}"
elif [ -d gold_patches ]; then
  PATCH_ROOT="gold_patches"
else
  PATCH_ROOT=""
fi

if [ -n "${PATCH_ROOT}" ]; then
  for patch_file in "${PATCH_ROOT}"/*.diff; do
    [ -e "${patch_file}" ] || continue
    patch --batch -p1 < "${patch_file}"
  done
elif [ -f gold-patch.diff ]; then
  patch --batch -p1 < gold-patch.diff
elif [ -f gold_patch.diff ]; then
  patch --batch -p1 < gold_patch.diff
else
  echo "No gold patch configured yet. Replace this placeholder solution." >&2
  exit 1
fi
