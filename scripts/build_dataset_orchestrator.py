#!/usr/bin/env python3
"""
build_dataset_orchestrator.py — Orchestrates parallel Paper DAG → LHB task builds.

Scans the paper_dag directory, launches independent Claude Code sessions per DAG
(up to N in parallel), tracks progress via status.json, and produces a summary report.

Usage:
    python3 scripts/build_dataset_orchestrator.py --parallelism 3
    python3 scripts/build_dataset_orchestrator.py --dag-dir /path/to/paper_dag --retry-failed
    python3 scripts/build_dataset_orchestrator.py --dag example_dag
"""

import argparse
import concurrent.futures
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
WORKSPACE_ROOT = REPO_ROOT.parent
TEMPLATE_PATH = SCRIPT_DIR / "templates" / "dag_builder_prompt.md"


def _env_path_str(name: str, default: Path) -> str:
    raw = os.environ.get(name)
    return str(Path(raw).expanduser() if raw else default)


DEFAULT_DAG_DIR = _env_path_str("LHB_PAPER_DAG_DIR", WORKSPACE_ROOT / "paper_dag")
DEFAULT_OUTPUT_DIR = _env_path_str("LHB_TASK_OUTPUT_DIR", REPO_ROOT / "tasks")
DEFAULT_PARALLELISM = 3
MAX_RETRIES = 2
DAG_TIMEOUT_SEC = 7200

ALL_STEPS = [
    "read_dag", "read_papers", "read_code", "download_base",
    "construct_base", "generate_patches", "extract_requirements",
    "generate_tests", "generate_metadata", "generate_docker",
    "generate_scripts", "validate",
]


def load_template() -> str:
    with open(TEMPLATE_PATH, "r") as f:
        return f.read()


def get_status(dag_dir: Path) -> dict | None:
    status_file = dag_dir / "status.json"
    if status_file.exists():
        with open(status_file) as f:
            return json.load(f)
    return None


def init_status(dag_dir: Path, dag_name: str, output_dir: str) -> dict:
    status = {
        "dag_name": dag_name,
        "task_dir": f"{output_dir}/task_{dag_name}",
        "status": "in_progress",
        "current_step": "read_dag",
        "steps_completed": [],
        "steps_total": ALL_STEPS,
        "last_error": None,
        "retry_count": 0,
        "started_at": datetime.datetime.now().isoformat(),
        "updated_at": datetime.datetime.now().isoformat(),
    }
    with open(dag_dir / "status.json", "w") as f:
        json.dump(status, f, indent=2)
    return status


def render_prompt(template: str, dag_dir: Path, dag_name: str, output_dir: str, status: dict | None) -> str:
    steps_completed = status["steps_completed"] if status else []
    resume_from = ""
    if status and status["status"] != "completed" and steps_completed:
        resume_from = status["current_step"]

    prompt = template
    prompt = prompt.replace("{{ dag_dir }}", str(dag_dir))
    prompt = prompt.replace("{{ output_dir }}", output_dir)
    prompt = prompt.replace("{{ dag_name }}", dag_name)

    # Handle conditional block
    if resume_from:
        # Keep the if-block content, remove else-block content
        prompt = prompt.replace("{% if resume_from %}", "")
        prompt = prompt.replace("{{ resume_from }}", resume_from)
        prompt = prompt.replace('{{ steps_completed | join(", ") }}', ", ".join(steps_completed))
        # Remove else block
        prompt = re.sub(
            r"\{% else %\}.*?\{% endif %\}",
            "",
            prompt,
            flags=re.DOTALL,
        )
    else:
        # Remove if-block content, keep else-block content
        prompt = re.sub(
            r"\{% if resume_from %\}.*?\{% else %\}",
            "",
            prompt,
            flags=re.DOTALL,
        )
        prompt = prompt.replace("{% endif %}", "")

    return prompt


def build_dag(dag_dir: Path, dag_name: str, output_dir: str, template: str) -> dict:
    log_file = dag_dir / "build.log"
    start_time = time.time()

    with open(log_file, "a") as log:
        log.write(f"\n{'='*60}\n")
        log.write(f"[{datetime.datetime.now().isoformat()}] BUILD START: {dag_name}\n")
        log.write(f"{'='*60}\n")

    status = get_status(dag_dir)
    if status is None:
        status = init_status(dag_dir, dag_name, output_dir)
    elif status["status"] == "failed":
        status["retry_count"] = status.get("retry_count", 0) + 1
        status["status"] = "in_progress"
        status["last_error"] = None
        status["updated_at"] = datetime.datetime.now().isoformat()
        with open(dag_dir / "status.json", "w") as f:
            json.dump(status, f, indent=2)

    prompt = render_prompt(template, dag_dir, dag_name, output_dir, status)

    task_output_dir = f"{output_dir}/task_{dag_name}"
    os.makedirs(task_output_dir, exist_ok=True)

    cmd = [
        "claude",
        "-p", prompt,
        "--dangerously-skip-permissions",
        "--model", "claude-opus-4.6",
        "--add-dir", str(dag_dir),
        "--add-dir", task_output_dir,
        "--add-dir", str(SCRIPT_DIR),
    ]

    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = "http://localhost:4141"
    env["ANTHROPIC_AUTH_TOKEN"] = "dummy"

    try:
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=DAG_TIMEOUT_SEC,
            cwd=str(SCRIPT_DIR.parent),
            env=env,
        )
        duration = time.time() - start_time

        with open(log_file, "a") as log:
            log.write(f"\n[STDOUT (last 5000 chars)]\n{result.stdout[-5000:]}\n")
            if result.stderr:
                log.write(f"\n[STDERR (last 2000 chars)]\n{result.stderr[-2000:]}\n")
            log.write(f"\n[EXIT CODE] {result.returncode}\n")
            log.write(f"[DURATION] {duration:.1f}s\n")

        final_status = get_status(dag_dir)
        if final_status and final_status["status"] == "completed":
            return {"dag": dag_name, "status": "completed", "duration_sec": round(duration)}
        else:
            error = final_status.get("last_error", "Unknown") if final_status else "No status"
            return {"dag": dag_name, "status": "failed", "error": error, "duration_sec": round(duration)}

    except subprocess.TimeoutExpired:
        duration = time.time() - start_time
        with open(log_file, "a") as log:
            log.write(f"\n[TIMEOUT] Exceeded {DAG_TIMEOUT_SEC}s\n")
        status = get_status(dag_dir) or {}
        status["status"] = "failed"
        status["last_error"] = f"Timeout after {DAG_TIMEOUT_SEC}s"
        status["updated_at"] = datetime.datetime.now().isoformat()
        with open(dag_dir / "status.json", "w") as f:
            json.dump(status, f, indent=2)
        return {"dag": dag_name, "status": "failed", "error": "timeout", "duration_sec": round(duration)}

    except Exception as e:
        duration = time.time() - start_time
        with open(log_file, "a") as log:
            log.write(f"\n[ERROR] {type(e).__name__}: {e}\n")
        return {"dag": dag_name, "status": "failed", "error": str(e), "duration_sec": round(duration)}


def discover_dags(dag_dir: Path) -> list[tuple[Path, str]]:
    dags = []
    if not dag_dir.exists():
        print(f"[ERROR] DAG directory does not exist: {dag_dir}", file=sys.stderr)
        sys.exit(1)
    for entry in sorted(dag_dir.iterdir()):
        if entry.is_dir() and (entry / "dag.json").exists():
            dags.append((entry, entry.name))
    return dags


def should_process(dag_dir: Path, retry_failed: bool) -> bool:
    status = get_status(dag_dir)
    if status is None:
        return True
    if status["status"] == "completed":
        return False
    if status["status"] == "failed":
        return retry_failed and status.get("retry_count", 0) < MAX_RETRIES
    return True  # in_progress → resume


def main():
    parser = argparse.ArgumentParser(description="Orchestrate parallel Paper DAG → LHB task builds")
    parser.add_argument("--parallelism", type=int, default=DEFAULT_PARALLELISM)
    parser.add_argument("--dag-dir", type=Path, default=Path(DEFAULT_DAG_DIR))
    parser.add_argument("--output-dir", type=str, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--dag", type=str, default=None)
    args = parser.parse_args()

    if not TEMPLATE_PATH.exists():
        print(f"[ERROR] Template not found: {TEMPLATE_PATH}", file=sys.stderr)
        sys.exit(1)
    template = load_template()

    all_dags = discover_dags(args.dag_dir)
    print(f"[INFO] Found {len(all_dags)} DAG(s) in {args.dag_dir}")

    if args.dag:
        all_dags = [(d, n) for d, n in all_dags if n == args.dag]
        if not all_dags:
            print(f"[ERROR] DAG '{args.dag}' not found", file=sys.stderr)
            sys.exit(1)

    dags_to_process = [(d, n) for d, n in all_dags if should_process(d, args.retry_failed)]
    skipped = len(all_dags) - len(dags_to_process)

    print(f"[INFO] Processing {len(dags_to_process)} DAG(s), skipping {skipped}")
    print(f"[INFO] Parallelism: {args.parallelism}")
    print(f"[INFO] Output: {args.output_dir}")
    print()

    if not dags_to_process:
        print("[INFO] Nothing to do.")
        return

    results = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.parallelism) as executor:
        future_to_dag = {}
        for dag_dir, dag_name in dags_to_process:
            print(f"[START] DAG: {dag_name}")
            future = executor.submit(build_dag, dag_dir, dag_name, args.output_dir, template)
            future_to_dag[future] = dag_name

        for future in concurrent.futures.as_completed(future_to_dag):
            dag_name = future_to_dag[future]
            try:
                result = future.result()
                results.append(result)
                if result["status"] == "completed":
                    print(f"[DONE] DAG: {dag_name} (duration: {result['duration_sec']}s)")
                else:
                    print(f"[FAIL] DAG: {dag_name} — {result.get('error', 'unknown')}")
            except Exception as e:
                results.append({"dag": dag_name, "status": "failed", "error": str(e), "duration_sec": 0})
                print(f"[FAIL] DAG: {dag_name} — {e}")

    # Report
    print()
    print("=" * 60)
    print("[REPORT] Build Summary")
    print("=" * 60)

    completed = sum(1 for r in results if r["status"] == "completed")
    failed = sum(1 for r in results if r["status"] == "failed")

    report = {
        "run_timestamp": datetime.datetime.now().isoformat(),
        "total_dags": len(all_dags),
        "processed": len(dags_to_process),
        "completed": completed,
        "failed": failed,
        "skipped": skipped,
        "results": results,
    }

    report_path = args.dag_dir / "build_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    print(f"  Total:     {len(all_dags)}")
    print(f"  Processed: {len(dags_to_process)}")
    print(f"  Completed: {completed}")
    print(f"  Failed:    {failed}")
    print(f"  Skipped:   {skipped}")
    print(f"  Report:    {report_path}")
    print()

    for r in results:
        icon = "+" if r["status"] == "completed" else "x"
        line = f"  [{icon}] {r['dag']}: {r['status']}"
        if r.get("error"):
            line += f" ({r['error'][:80]})"
        if r.get("duration_sec"):
            line += f" [{r['duration_sec']}s]"
        print(line)

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
