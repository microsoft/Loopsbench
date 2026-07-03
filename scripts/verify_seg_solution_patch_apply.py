#!/usr/bin/env python3
"""
Strictly verify that a seg task's gold patches apply in solution order on hollow base.

Unlike solution.sh (often `|| true` / check=False), this script fails on the first
`git apply` non-zero exit so you can answer: "does the full ordered chain actually apply?"

Usage:
  python3 scripts/verify_seg_solution_patch_apply.py --task-dir tasks/task_django_seg01
  python3 scripts/verify_seg_solution_patch_apply.py --all-seg
"""
from __future__ import annotations

import argparse
import re
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
    get_pr_order,
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


def _seg_task_dirs(tasks_dir: Path) -> list[Path]:
    out: list[Path] = []
    for p in sorted(tasks_dir.glob("task_*_seg[0-9][0-9]")):
        if p.is_dir() and (p / "solution.sh").is_file():
            out.append(p)
    return out


def verify_one(task_dir: Path, keep_tmp: Path | None = None) -> tuple[bool, str]:
    task_dir = task_dir.resolve()
    base = task_dir / "base"
    if not base.is_dir():
        return False, "no base/ directory"

    try:
        pr_order = get_pr_order(task_dir)
    except SystemExit as e:
        return False, f"get_pr_order exited: {e}"
    except Exception as e:
        return False, f"get_pr_order failed: {e}"

    if not pr_order:
        return False, "empty PR order"

    tmp_parent = keep_tmp or Path(tempfile.mkdtemp(prefix="lhb-seg-verify-"))
    work = tmp_parent / "workspace"
    shutil.copytree(base, work, dirs_exist_ok=True, symlinks=True)

    _git(work, "init", "-q")
    _git(work, "config", "user.email", "verify@lhb.invalid")
    _git(work, "config", "user.name", "LHB verify")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "base", "--allow-empty")

    repo_root = str(work.resolve())

    for i, pr_num in enumerate(pr_order):
        patch_path = resolve_patch_host_path(task_dir, pr_num)
        if patch_path is None or not patch_path.is_file():
            return False, f"missing patch file for unit {pr_num!r} (index {i})"

        apply_cwd = Path(infer_patch_apply_cwd(task_dir, repo_root))
        if not apply_cwd.is_dir():
            return False, f"apply cwd missing: {apply_cwd} (unit {pr_num})"

        r = subprocess.run(
            ["git", "apply", "--allow-empty", str(patch_path)],
            cwd=str(apply_cwd),
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip()[-800:]
            return False, f"git apply failed at index {i} unit {pr_num!r} rc={r.returncode}\n{tail}"

        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", f"unit {pr_num}", "--allow-empty")

    if keep_tmp is None:
        shutil.rmtree(tmp_parent, ignore_errors=True)

    return True, f"ok {len(pr_order)} patches"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-dir", type=Path, help="Single seg task directory")
    ap.add_argument("--all-seg", action="store_true", help="All task_*_segNN under tasks/")
    ap.add_argument("--keep-tmp", type=Path, help="Keep clone under this path (debug)")
    args = ap.parse_args()

    if bool(args.task_dir) == bool(args.all_seg):
        ap.error("Specify exactly one of --task-dir or --all-seg")

    tasks_dir = ROOT / "tasks"
    if args.all_seg:
        dirs = _seg_task_dirs(tasks_dir)
    else:
        d = args.task_dir.resolve()
        if not d.is_dir():
            print(f"FAIL not a directory: {d}", file=sys.stderr)
            sys.exit(2)
        name = d.name
        if not re.match(r"task_.+_seg\d+$", name):
            print(f"WARN {name} does not look like a seg task", file=sys.stderr)
        dirs = [d]

    failed = 0
    for d in dirs:
        ok, msg = verify_one(d, keep_tmp=args.keep_tmp)
        label = "PASS" if ok else "FAIL"
        print(f"{label}\t{d.name}\t{msg}")
        if not ok:
            failed += 1

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
