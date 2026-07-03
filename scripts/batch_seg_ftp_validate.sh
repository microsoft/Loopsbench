#!/usr/bin/env bash
# Strict FTP (fail-to-pass) for seg tasks only: task_*_segNN
#
# Usage:
#   bash scripts/batch_seg_ftp_validate.sh           # default -j 2
#   bash scripts/batch_seg_ftp_validate.sh -j 1      # sequential
#   bash scripts/batch_seg_ftp_validate.sh -j 4      # more parallelism (heavy on Docker)

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TASKS_DIR="${PROJECT_ROOT}/tasks"
VALIDATE_SCRIPT="${PROJECT_ROOT}/scripts/validate_per_pr.py"

if [[ -x "${PROJECT_ROOT}/.venv/bin/python" ]]; then
  PYTHON="${PROJECT_ROOT}/.venv/bin/python"
else
  PYTHON="python3"
fi

MAX_JOBS=2
while getopts "j:" opt; do
  case "${opt}" in
    j) MAX_JOBS="${OPTARG}" ;;
    *) echo "Usage: $0 [-j N]" >&2; exit 1 ;;
  esac
done

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
RUN_DIR="${PROJECT_ROOT}/runs/seg-ftp-validate-${TIMESTAMP}"
LOG_DIR="${RUN_DIR}/logs"
JSON_DIR="${RUN_DIR}/json"
RESULT_DIR="${RUN_DIR}/.results"
mkdir -p "${LOG_DIR}" "${JSON_DIR}" "${RESULT_DIR}"

TASK_LIST=()
for task_path in "${TASKS_DIR}"/task_*_seg[0-9][0-9]/; do
  [[ -d "${task_path}" ]] || continue
  task_id=$(basename "${task_path}")
  TASK_LIST+=("${task_id}")
done
_sorted=()
while IFS= read -r _line; do _sorted+=("$_line"); done < <(printf '%s\n' "${TASK_LIST[@]}" | sort)
TASK_LIST=("${_sorted[@]}")

TOTAL=${#TASK_LIST[@]}
if [[ "${TOTAL}" -eq 0 ]]; then
  echo "No seg tasks under ${TASKS_DIR}/task_*_segNN/" >&2
  exit 1
fi

echo "============================================"
echo "  LHB Seg-only strict FTP"
echo "  Started:     $(date -Iseconds)"
echo "  Concurrency: ${MAX_JOBS}"
echo "  Tasks:       ${TOTAL}"
echo "  Python:      ${PYTHON}"
echo "  Validator:   ${VALIDATE_SCRIPT}"
echo "  Output:      ${RUN_DIR}"
echo "============================================"

run_one_task() {
  local task_id="$1"
  local task_path="${TASKS_DIR}/${task_id}"
  local log_file="${LOG_DIR}/${task_id}.log"
  local json_file="${JSON_DIR}/${task_id}.json"
  local result_file="${RESULT_DIR}/${task_id}"
  local exit_code=0
  "${PYTHON}" "${VALIDATE_SCRIPT}" \
    --task-dir "${task_path}" \
    --strict-fail-to-pass \
    --json-out "${json_file}" \
    >"${log_file}" 2>&1 || exit_code=$?
  echo "${exit_code}" >"${result_file}"
}

export -f run_one_task
export TASKS_DIR LOG_DIR JSON_DIR RESULT_DIR PYTHON VALIDATE_SCRIPT

RUNNING=0
PIDS=()

for i in "${!TASK_LIST[@]}"; do
  task_id="${TASK_LIST[$i]}"
  seq_num=$((i + 1))
  echo "[${seq_num}/${TOTAL}] Starting ${task_id} ..."

  run_one_task "${task_id}" &
  pid=$!
  PIDS+=("${pid}")
  RUNNING=$((RUNNING + 1))

  if [[ "${RUNNING}" -ge "${MAX_JOBS}" ]]; then
    wait -n 2>/dev/null || true
    RUNNING=$((RUNNING - 1))
  fi
done

for pid in "${PIDS[@]}"; do
  wait "${pid}" 2>/dev/null || true
done

PASSED=()
FAILED=()
ERRORED=()

for task_id in "${TASK_LIST[@]}"; do
  result_file="${RESULT_DIR}/${task_id}"
  if [[ ! -f "${result_file}" ]]; then
    ERRORED+=("${task_id}")
    continue
  fi
  exit_code=$(cat "${result_file}")
  if [[ "${exit_code}" -eq 0 ]]; then
    PASSED+=("${task_id}")
  elif [[ "${exit_code}" -eq 1 ]]; then
    FAILED+=("${task_id}")
  else
    ERRORED+=("${task_id}")
  fi
done

SUMMARY_FILE="${RUN_DIR}/summary.txt"
{
  echo "LHB Seg strict FTP summary"
  echo "Date: $(date -Iseconds)"
  echo "Concurrency: ${MAX_JOBS}"
  echo "Total: ${TOTAL}"
  echo "Passed: ${#PASSED[@]}"
  echo "Failed: ${#FAILED[@]}"
  echo "Errored: ${#ERRORED[@]}"
  echo ""
  if [[ ${#PASSED[@]} -gt 0 ]]; then
    echo "--- PASSED (${#PASSED[@]}) ---"
    printf '  %s\n' "${PASSED[@]}"
    echo ""
  fi
  if [[ ${#FAILED[@]} -gt 0 ]]; then
    echo "--- FAILED (${#FAILED[@]}) ---"
    printf '  %s\n' "${FAILED[@]}"
    echo ""
  fi
  if [[ ${#ERRORED[@]} -gt 0 ]]; then
    echo "--- ERRORED (${#ERRORED[@]}) ---"
    printf '  %s\n' "${ERRORED[@]}"
    echo ""
  fi
} | tee "${SUMMARY_FILE}"

echo ""
echo "Logs:    ${LOG_DIR}/<task_id>.log"
echo "JSON:    ${JSON_DIR}/<task_id>.json"
echo "Summary: ${SUMMARY_FILE}"

rm -rf "${RESULT_DIR}"

if [[ ${#FAILED[@]} -gt 0 ]] || [[ ${#ERRORED[@]} -gt 0 ]]; then
  exit 1
fi
