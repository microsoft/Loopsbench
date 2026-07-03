#!/usr/bin/env bash
set -euo pipefail

# ===========================================================================
# refine_tasks_parallel.sh
#
# Run the /lhb-task-refine skill on all eligible (non-seg) LHB tasks
# in parallel, each in its own independent Claude Code context window.
#
# Usage:
#   ./scripts/refine_tasks_parallel.sh [OPTIONS]
#
# Options:
#   -j, --jobs N         Max parallel Claude processes (default: 4)
#   -t, --task PATTERN   Only run tasks matching this glob (e.g., "task_rv64*")
#   -l, --list           List eligible tasks and exit (dry run)
#   -m, --model MODEL    Claude model to use (default: sonnet)
#   -o, --output DIR     Log output directory (default: ./refine_logs/<timestamp>)
#   -h, --help           Show this help
#
# Requirements:
#   - 'claude' CLI must be on PATH
#   - Working directory should be the Long-Horizon-Bench repo root
# ===========================================================================

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TASKS_DIR="$REPO_ROOT/tasks"
MAX_JOBS=4
TASK_FILTER=""
LIST_ONLY=false
MODEL="claude-opus-4-7"
LOG_DIR=""

usage() {
    sed -n '2,/^# =====/{ /^# =====/d; s/^# \?//; p }' "$0"
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -j|--jobs)    MAX_JOBS="$2"; shift 2 ;;
        -t|--task)    TASK_FILTER="$2"; shift 2 ;;
        -l|--list)    LIST_ONLY=true; shift ;;
        -m|--model)   MODEL="$2"; shift 2 ;;
        -o|--output)  LOG_DIR="$2"; shift 2 ;;
        -h|--help)    usage ;;
        *)            echo "Unknown option: $1"; usage ;;
    esac
done

# Default log directory
if [[ -z "$LOG_DIR" ]]; then
    LOG_DIR="$REPO_ROOT/refine_logs/$(date +%Y%m%d_%H%M%S)"
fi

# ---------------------------------------------------------------------------
# Collect eligible tasks (apply suffix exclusions)
# ---------------------------------------------------------------------------
collect_tasks() {
    local tasks=()
    for d in "$TASKS_DIR"/task_*/; do
        [[ ! -d "$d" ]] && continue
        local name
        name=$(basename "$d")

        # Skip seg tasks (_seg01 .. _seg20)
        if [[ "$name" =~ _seg[0-9]{2}$ ]]; then
            continue
        fi

        # Skip *_medium and *_hard
        if [[ "$name" =~ _(medium|hard)$ ]]; then
            continue
        fi

        # Skip *_evolution
        if [[ "$name" =~ _evolution$ ]]; then
            continue
        fi

        # Skip *_glucose
        if [[ "$name" =~ _glucose$ ]]; then
            continue
        fi

        # Apply user filter if provided
        if [[ -n "$TASK_FILTER" ]]; then
            # shellcheck disable=SC2254
            case "$name" in
                $TASK_FILTER) ;;  # matches
                *) continue ;;    # doesn't match
            esac
        fi

        tasks+=("$d")
    done
    printf '%s\n' "${tasks[@]}"
}

TASKS=()
while IFS= read -r line; do
    [[ -n "$line" ]] && TASKS+=("$line")
done < <(collect_tasks)

echo "Found ${#TASKS[@]} eligible tasks"

if [[ ${#TASKS[@]} -eq 0 ]]; then
    echo "No tasks matched. Check --task filter or tasks/ directory."
    exit 1
fi

if $LIST_ONLY; then
    for t in "${TASKS[@]}"; do
        echo "  $(basename "$t")"
    done
    exit 0
fi

# ---------------------------------------------------------------------------
# Prepare log directory
# ---------------------------------------------------------------------------
mkdir -p "$LOG_DIR"
echo "Logs: $LOG_DIR"
echo "Parallelism: $MAX_JOBS"
echo "Model: $MODEL"
echo "---"

# ---------------------------------------------------------------------------
# Worker function — runs one Claude session per task
# ---------------------------------------------------------------------------
run_one_task() {
    local task_dir="$1"
    local task_name
    task_name=$(basename "$task_dir")
    local logfile="$LOG_DIR/${task_name}.log"
    local status_file="$LOG_DIR/${task_name}.status"

    echo "[START] $task_name (log: $logfile)"

    local start_ts
    start_ts=$(date +%s)

    # Invoke Claude in non-interactive mode with the skill
    # Each invocation is a completely independent context window
    if claude -p "/lhb-task-refine ${task_dir}" \
        --model "$MODEL" \
        --allowedTools "Edit,Write,Read,Bash,Glob,Grep" \
        > "$logfile" 2>&1; then
        local end_ts
        end_ts=$(date +%s)
        local elapsed=$(( end_ts - start_ts ))
        echo "OK" > "$status_file"
        echo "[DONE]  $task_name  (${elapsed}s)"
    else
        local exit_code=$?
        local end_ts
        end_ts=$(date +%s)
        local elapsed=$(( end_ts - start_ts ))
        echo "FAIL:$exit_code" > "$status_file"
        echo "[FAIL]  $task_name  (${elapsed}s, exit=$exit_code)"
    fi
}

export -f run_one_task
export LOG_DIR MODEL

# ---------------------------------------------------------------------------
# Parallel execution
# ---------------------------------------------------------------------------
echo "Starting parallel refine (${#TASKS[@]} tasks, max $MAX_JOBS concurrent)..."
echo ""

# Use xargs for portable parallelism
# Each task path is on its own line; xargs -P controls concurrency
printf '%s\n' "${TASKS[@]}" | xargs -P "$MAX_JOBS" -I {} bash -c 'run_one_task "$@"' _ {}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "========================================="
echo "  Refine Summary"
echo "========================================="

ok_count=0
fail_count=0
fail_list=()

for t in "${TASKS[@]}"; do
    task_name=$(basename "$t")
    status_file="$LOG_DIR/${task_name}.status"
    if [[ -f "$status_file" ]] && grep -q "^OK$" "$status_file"; then
        (( ok_count++ )) || true
    else
        (( fail_count++ )) || true
        fail_list+=("$task_name")
    fi
done

echo "  Total:  ${#TASKS[@]}"
echo "  OK:     $ok_count"
echo "  Failed: $fail_count"

if [[ ${#fail_list[@]} -gt 0 ]]; then
    echo ""
    echo "  Failed tasks:"
    for f in "${fail_list[@]}"; do
        echo "    - $f (see $LOG_DIR/${f}.log)"
    done
fi

echo ""
echo "Full logs: $LOG_DIR/"

# Write machine-readable summary
python3 -c "
import json, os, sys

log_dir = '$LOG_DIR'
tasks = [os.path.basename(t.strip('/')) for t in '''$(printf '%s\n' "${TASKS[@]}")'''.strip().split('\n')]
results = {}
for t in tasks:
    sf = os.path.join(log_dir, t + '.status')
    if os.path.isfile(sf):
        results[t] = open(sf).read().strip()
    else:
        results[t] = 'NO_STATUS'

summary = {
    'total': len(tasks),
    'ok': sum(1 for v in results.values() if v == 'OK'),
    'failed': sum(1 for v in results.values() if v != 'OK'),
    'results': results
}
with open(os.path.join(log_dir, 'summary.json'), 'w') as f:
    json.dump(summary, f, indent=2)
print(f'Summary written to {log_dir}/summary.json')
" 2>/dev/null || true
