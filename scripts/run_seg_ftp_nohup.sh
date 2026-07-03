#!/usr/bin/env bash
# One-shot: full seg strict FTP in background with logs (run from a real terminal, not IDE sandbox).
#
# Usage:
#   bash scripts/run_seg_ftp_nohup.sh           # -j 2, log /tmp/seg-ftp-full.log
#   bash scripts/run_seg_ftp_nohup.sh -j 1    # sequential
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
MAX_JOBS=2
while getopts "j:" opt; do
  case "${opt}" in
    j) MAX_JOBS="${OPTARG}" ;;
    *) echo "Usage: $0 [-j N]" >&2; exit 1 ;;
  esac
done
LOG="${SEG_FTP_LOG:-/tmp/seg-ftp-full.log}"
PIDF="${SEG_FTP_PID:-/tmp/seg-ftp-full.pid}"
nohup bash scripts/batch_seg_ftp_validate.sh -j "${MAX_JOBS}" >"${LOG}" 2>&1 &
echo $! >"${PIDF}"
echo "Started seg strict FTP batch (PID $(cat "${PIDF}"), -j ${MAX_JOBS})."
echo "  Log:   ${LOG}"
echo "  PID:   ${PIDF}"
echo "  Tail:  tail -f ${LOG}"
echo "Results directory is printed at the top of the log (runs/seg-ftp-validate-<timestamp>/)."
