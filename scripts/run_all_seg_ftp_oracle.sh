#!/usr/bin/env bash
set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR" || exit 1

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <run-root>" >&2
  exit 2
fi

RUN_ROOT="$1"
mkdir -p "$RUN_ROOT"/{ftp_json,oracle_runs,status}

TASKS=(
  task_ClickHouse_seg07
  task_NodeBB_seg01
  task_NodeBB_seg03
  task_NodeBB_seg05
  task_TypeScript_seg01
  task_ansible_seg11
  task_ansible_seg13
  task_django_seg01
  task_django_seg05
  task_django_seg11
  task_echarts_seg01
  task_echarts_seg04
  task_echarts_seg05
  task_echarts_seg07
  task_echarts_seg08
  task_echarts_seg09
  task_echarts_seg10
  task_echarts_seg11
  task_echarts_seg13
  task_framework_seg04
  task_hadoop_seg05
  task_jenkins_seg02
  task_jenkins_seg05
  task_navidrome_seg02
  task_navidrome_seg04
  task_navidrome_seg05
  task_navidrome_seg07
  task_node_seg03
  task_rails_seg09
)

SUMMARY_TSV="$RUN_ROOT/status/summary.tsv"
{
  printf "task\tftp_exit\toracle_exit\tftp_json\toracle_dir\n"
} > "$SUMMARY_TSV"

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

echo "[$(timestamp)] run_root=$RUN_ROOT"
echo "[$(timestamp)] task_count=${#TASKS[@]}"

for task in "${TASKS[@]}"; do
  ftp_exit=""
  oracle_exit=""
  ftp_json="$RUN_ROOT/ftp_json/${task}.json"
  oracle_dir="$RUN_ROOT/oracle_runs/${task}"
  mkdir -p "$oracle_dir"

  echo "[$(timestamp)] [FTP] start $task"
  python3 scripts/validate_per_pr.py \
    --task-dir "tasks/${task}" \
    --strict-fail-to-pass \
    --json-out "$ftp_json"
  ftp_exit=$?
  echo "[$(timestamp)] [FTP] done $task exit=$ftp_exit"

  echo "[$(timestamp)] [ORACLE] start $task"
  lhb run \
    --agent oracle \
    --dataset-path tasks \
    --task-id "$task" \
    --output-path "$oracle_dir" \
    --n-concurrent 1 \
    --n-attempts 1 \
    --no-livestream
  oracle_exit=$?
  echo "[$(timestamp)] [ORACLE] done $task exit=$oracle_exit"

  printf "%s\t%s\t%s\t%s\t%s\n" \
    "$task" "$ftp_exit" "$oracle_exit" "$ftp_json" "$oracle_dir" >> "$SUMMARY_TSV"
done

echo "[$(timestamp)] finished"
