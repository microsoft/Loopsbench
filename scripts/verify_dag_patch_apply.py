#!/usr/bin/env python3
"""
Verify that a task's gold patches apply cleanly in strict DAG topological order.

This script ignores solution.sh ordering and applies patches purely based on
unit_dag.json. Useful for validating that the DAG order itself is consistent
with patch applicability.

Usage:
  python3 scripts/verify_dag_patch_apply.py --task-dir tasks/task_panda3d_collision_hard
  python3 scripts/verify_dag_patch_apply.py --all
  python3 scripts/verify_dag_patch_apply.py --all --filter '*_medium' --filter '*_hard'
"""
from __future__ import annotations

import argparse
import fnmatch
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent
for p in (ROOT, SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from validate_per_pr import (  # noqa: E402
    _get_topological_pr_order,
    infer_patch_apply_cwd,
    resolve_patch_host_path,
)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _collect_task_dirs(tasks_dir: Path, filters: list[str] | None) -> list[Path]:
    out: list[Path] = []
    for p in sorted(tasks_dir.glob("task_*")):
        if not p.is_dir():
            continue
        if not (p / "gold_patches").is_dir():
            continue
        if filters:
            if not any(fnmatch.fnmatch(p.name, f) for f in filters):
                continue
        out.append(p)
    return out


def verify_one(task_dir: Path, keep_tmp: Path | None = None, verbose: bool = False) -> tuple[bool, str]:
    task_dir = task_dir.resolve()
    base = task_dir / "base"
    if not base.is_dir():
        return False, "no base/ directory"

    dag_order = _get_topological_pr_order(task_dir)
    if not dag_order:
        return False, "no DAG order (missing/empty unit_dag.json or no patches)"

    tmp_parent = keep_tmp or Path(tempfile.mkdtemp(prefix="lhb-dag-verify-"))
    work = tmp_parent / "workspace"
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(base, work, dirs_exist_ok=True, symlinks=True)

    _git(work, "init", "-q")
    _git(work, "config", "user.email", "verify@lhb.invalid")
    _git(work, "config", "user.name", "LHB verify")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "base", "--allow-empty")

    repo_root = str(work.resolve())
    applied = 0
    skipped = 0

    for i, unit_id in enumerate(dag_order):
        patch_path = resolve_patch_host_path(task_dir, unit_id)
        if patch_path is None or not patch_path.is_file():
            return False, f"missing patch file for unit {unit_id!r} (DAG index {i})"

        if patch_path.stat().st_size == 0:
            skipped += 1
            if verbose:
                print(f"  [skip] {unit_id} (empty patch)")
            continue

        apply_cwd = Path(infer_patch_apply_cwd(task_dir, repo_root))
        if not apply_cwd.is_dir():
            return False, f"apply cwd missing: {apply_cwd} (unit {unit_id})"

        r = subprocess.run(
            ["git", "apply", "--allow-empty", str(patch_path)],
            cwd=str(apply_cwd),
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip()[-800:]
            return False, f"git apply failed at DAG index {i} unit {unit_id!r} rc={r.returncode}\n{tail}"

        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", f"unit {unit_id}", "--allow-empty")
        applied += 1

    if keep_tmp is None:
        shutil.rmtree(tmp_parent, ignore_errors=True)

    return True, f"ok {applied} applied, {skipped} skipped (empty), {len(dag_order)} total"


def main() -> None:
    ap = argparse.ArgumentParser(description="Verify gold patches apply in DAG topological order")
    ap.add_argument("--task-dir", type=Path, help="Single task directory")
    ap.add_argument("--all", action="store_true", help="All tasks with gold_patches/ under tasks/")
    ap.add_argument("--filter", action="append", dest="filters", help="Glob filter on task dir name (repeatable)")
    ap.add_argument("--keep-tmp", type=Path, help="Keep working clone under this path (debug)")
    ap.add_argument("--verbose", "-v", action="store_true", help="Show per-patch progress")
    args = ap.parse_args()

    if bool(args.task_dir) == bool(args.all):
        ap.error("Specify exactly one of --task-dir or --all")

    tasks_dir = ROOT / "tasks"
    if args.all:
        dirs = _collect_task_dirs(tasks_dir, args.filters)
    else:
        d = args.task_dir.resolve()
        if not d.is_dir():
            print(f"FAIL not a directory: {d}", file=sys.stderr)
            sys.exit(2)
        dirs = [d]

    passed = 0
    failed = 0
    for d in dirs:
        ok, msg = verify_one(d, keep_tmp=args.keep_tmp, verbose=args.verbose)
        label = "PASS" if ok else "FAIL"
        print(f"{label}\t{d.name}\t{msg}")
        if ok:
            passed += 1
        else:
            failed += 1

    if len(dirs) > 1:
        print(f"\nTotal: {len(dirs)}  Passed: {passed}  Failed: {failed}")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
