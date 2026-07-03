#!/usr/bin/env bash
set -euo pipefail

# Batch FTP (fail-to-pass) validation for all LHB tasks.
# Runs validate_per_pr.py on each task with configurable concurrency,
# writes per-task logs, and collects a summary of pass/fail results.
#
# Usage:
#   bash scripts/batch_ftp_validate.sh           # default: 4 concurrent
#   bash scripts/batch_ftp_validate.sh -j 8      # 8 concurrent
#   bash scripts/batch_ftp_validate.sh -j 1      # sequential

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VALIDATE_SCRIPT="${PROJECT_ROOT}/scripts/validate_per_pr.py"
TASKS_DIR="${PROJECT_ROOT}/tasks"
PYTHON="${PROJECT_ROOT}/.venv/bin/python"

MAX_JOBS=4

while getopts "j:" opt; do
    case ${opt} in
        j) MAX_JOBS="${OPTARG}" ;;
        *) echo "Usage: $0 [-j N]" >&2; exit 1 ;;
    esac
done

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
RUN_DIR="${PROJECT_ROOT}/runs/ftp-validate-${TIMESTAMP}"
LOG_DIR="${RUN_DIR}/logs"
JSON_DIR="${RUN_DIR}/json"
RESULT_DIR="${RUN_DIR}/.results"

mkdir -p "${LOG_DIR}" "${JSON_DIR}" "${RESULT_DIR}"

TASK_LIST=()
for task_path in "${TASKS_DIR}"/task_*/; do
    [ -d "${task_path}" ] || continue
    task_id=$(basename "${task_path}")
    [[ "${task_id}" == .* ]] && continue
    TASK_LIST+=("${task_id}")
done

TOTAL=${#TASK_LIST[@]}

echo "============================================"
echo "  LHB Batch FTP Validation"
echo "  Started:     $(date)"
echo "  Concurrency: ${MAX_JOBS}"
echo "  Tasks:       ${TOTAL}"
echo "  Output:      ${RUN_DIR}"
echo "============================================"
echo ""

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
        > "${log_file}" 2>&1 || exit_code=$?

    echo "${exit_code}" > "${result_file}"
}

export -f run_one_task
export TASKS_DIR LOG_DIR JSON_DIR RESULT_DIR PYTHON VALIDATE_SCRIPT

RUNNING=0
PIDS=()
PIDMAP=()

for i in "${!TASK_LIST[@]}"; do
    task_id="${TASK_LIST[$i]}"
    seq_num=$((i + 1))

    echo "[${seq_num}/${TOTAL}] Starting ${task_id} ..."

    run_one_task "${task_id}" &
    pid=$!
    PIDS+=("${pid}")
    PIDMAP+=("${pid}:${task_id}")
    RUNNING=$((RUNNING + 1))

    if [ "${RUNNING}" -ge "${MAX_JOBS}" ]; then
        wait -n 2>/dev/null || true
        RUNNING=$((RUNNING - 1))
    fi
done

for pid in "${PIDS[@]}"; do
    wait "${pid}" 2>/dev/null || true
done

echo ""
echo "All tasks finished. Collecting results..."
echo ""

PASSED=()
FAILED=()
ERRORED=()

for task_id in "${TASK_LIST[@]}"; do
    result_file="${RESULT_DIR}/${task_id}"
    if [ ! -f "${result_file}" ]; then
        ERRORED+=("${task_id}")
        continue
    fi
    exit_code=$(cat "${result_file}")
    if [ "${exit_code}" -eq 0 ]; then
        PASSED+=("${task_id}")
    elif [ "${exit_code}" -eq 1 ]; then
        FAILED+=("${task_id}")
    else
        ERRORED+=("${task_id}")
    fi
done

echo "============================================"
echo "  FTP Validation Summary"
echo "  Finished: $(date)"
echo "============================================"
echo ""
echo "Total:   ${TOTAL}"
echo "Passed:  ${#PASSED[@]}"
echo "Failed:  ${#FAILED[@]}"
echo "Errored: ${#ERRORED[@]}"
echo ""

SUMMARY_FILE="${RUN_DIR}/summary.txt"
{
    echo "LHB Batch FTP Validation Summary"
    echo "================================"
    echo "Date: $(date)"
    echo "Concurrency: ${MAX_JOBS}"
    echo "Total: ${TOTAL}"
    echo "Passed: ${#PASSED[@]}"
    echo "Failed: ${#FAILED[@]}"
    echo "Errored: ${#ERRORED[@]}"
    echo ""

    if [ ${#PASSED[@]} -gt 0 ]; then
        echo "--- PASSED (${#PASSED[@]}) ---"
        for t in "${PASSED[@]}"; do echo "  ${t}"; done
        echo ""
    fi

    if [ ${#FAILED[@]} -gt 0 ]; then
        echo "--- FAILED (${#FAILED[@]}) ---"
        for t in "${FAILED[@]}"; do echo "  ${t}"; done
        echo ""
    fi

    if [ ${#ERRORED[@]} -gt 0 ]; then
        echo "--- ERRORED (${#ERRORED[@]}) ---"
        for t in "${ERRORED[@]}"; do echo "  ${t}"; done
        echo ""
    fi
} | tee "${SUMMARY_FILE}"

echo ""
echo "Logs:    ${LOG_DIR}/<task_id>.log"
echo "JSON:    ${JSON_DIR}/<task_id>.json"
echo "Summary: ${SUMMARY_FILE}"

rm -rf "${RESULT_DIR}"

if [ ${#FAILED[@]} -gt 0 ] || [ ${#ERRORED[@]} -gt 0 ]; then
    exit 1
fi
