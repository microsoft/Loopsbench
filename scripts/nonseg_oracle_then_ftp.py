#!/usr/bin/env python3
"""
对「非排除」tasks 按技能 lhb-nonseg-oracle-dag-ftp 顺序处理：

  步骤 1 — 检查 gold_patches/*.diff 是否缺失或 0 字节
  步骤 2 — 若缺失/空：调用 split_gold_patch_by_dag.py；随后运行 verify_gold_shards_vs_monolithic.py（技能 2.1，禁止脚本批量补全分片；未通过则由 Agent 参照整包 gold 与 base 分段生成 patch）
  步骤 3 — lhb Oracle（不以是否 resolved 阻断后续步骤）
  步骤 4 — validate_per_pr.py --strict-fail-to-pass（除非 --skip-ftp）

  默认：若 gold_patches 在步骤 2 末尾仍不完整，或 verify_gold_shards_vs_monolithic 失败，则不执行步骤 3/4（对齐技能「步骤 2→3 门禁」）。

过滤规则与技能一致。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SEG = re.compile(r"_seg(0[1-9]|1[0-9]|20)$")


def eligible_task_dir(name: str) -> bool:
    if not name.startswith("task_"):
        return False
    if name == "task_sat_minisat_glucose":
        return False
    if SEG.search(name):
        return False
    if name.endswith("_medium") or name.endswith("_hard"):
        return False
    if name.endswith("_evolution"):
        return False
    return True


def list_tasks(tasks_root: Path) -> list[str]:
    return sorted(p.name for p in tasks_root.iterdir() if p.is_dir() and eligible_task_dir(p.name))


def _split_noop_stems(task_dir: Path) -> set[str]:
    p = task_dir / "gold_patches" / ".lhb_split_noop.json"
    if not p.is_file():
        return set()
    try:
        data = json.loads(p.read_text())
    except Exception:
        return set()
    if not isinstance(data, dict):
        return set()
    raw = data.get("noop_empty_units") or []
    if not isinstance(raw, list):
        return set()
    return {str(x).strip() for x in raw if str(x).strip()}


def gold_patches_nonempty(task_dir: Path) -> tuple[bool, str]:
    """存在 gold_patches/、每个单元有 *.diff，且无非预期的 0 字节分片。"""
    gp = task_dir / "gold_patches"
    if not gp.is_dir():
        return False, "missing gold_patches/"
    diffs = sorted(gp.glob("*.diff"))
    if not diffs:
        return False, "no *.diff under gold_patches/"
    allowed_empty = _split_noop_stems(task_dir)
    empty = [p.name for p in diffs if p.stat().st_size == 0]
    unexpected = [n for n in empty if Path(n).stem not in allowed_empty]
    if unexpected:
        return False, f"empty diff(s) (not in split noop list): {', '.join(unexpected)}"
    return True, ""


def _oracle_cmd_env(repo: Path, task_id: str, run_id: str, output_path: Path) -> tuple[list[str], dict[str, str]]:
    """Prefer `lhb` on PATH; else run Typer app via repo-root PYTHONPATH + cli/main.py."""
    tail = [
        "run",
        "--agent",
        "oracle",
        # Harness default outer_loop_count=3 splits agent budget per round; Oracle runs
        # solution.sh once and must not retry — use full max_agent_timeout_sec in round 1.
        "--agent-kwarg",
        "outer_loop_count=1",
        "--task-id",
        task_id,
        "--dataset-path",
        "tasks",
        "--run-id",
        run_id,
        "--output-path",
        str(output_path),
    ]
    exe = shutil.which("lhb")
    if exe:
        return [exe, *tail], {**os.environ}
    pp = str(repo)
    prev = os.environ.get("PYTHONPATH", "")
    if prev:
        pp = pp + os.pathsep + prev
    env = {**os.environ, "PYTHONPATH": pp}
    return [sys.executable, str(repo / "long_horizon_bench/cli/main.py"), *tail], env


def oracle_resolved(results_path: Path) -> bool | None:
    if not results_path.is_file():
        return None
    try:
        data = json.loads(results_path.read_text())
    except Exception:
        return None
    for row in data.get("results") or []:
        if row.get("task_id") and "is_resolved" in row:
            v = row.get("is_resolved")
            if v is None:
                return None
            return bool(v)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", type=Path, default=Path(os.environ.get("LHB_REPO_ROOT", ".")).resolve())
    ap.add_argument("--output-path", type=Path, default=Path("/tmp/lhb_nonseg_oracle_runs"))
    ap.add_argument("--start", type=int, default=0, help="从排序后列表的第 N 个任务开始（0-based）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少个任务，0 表示不限制")
    ap.add_argument(
        "--task-ids",
        type=str,
        default="",
        help="仅处理这些 task-id（逗号分隔）；若设置则忽略 --start/--limit 对队列的切片",
    )
    ap.add_argument("--skip-ftp", action="store_true", help="步骤 4 不跑 validate_per_pr strict FTP")
    ap.add_argument(
        "--skip-empty-gold-patches",
        action="store_true",
        help="（已弃用）gold 不完整时跳过 3/4 现为默认行为；保留本开关仅为兼容旧命令行",
    )
    ap.add_argument(
        "--continue-with-incomplete-gold-patches",
        action="store_true",
        help="在 gold_patches 仍不完整（含未登记空分片）时仍执行步骤 3/4（旧行为；默认关闭）",
    )
    ap.add_argument(
        "--list-only",
        action="store_true",
        help="只打印将处理的任务 id（应用筛选后），不执行后续步骤",
    )
    ap.add_argument(
        "--continue-after-shard-verify-fail",
        action="store_true",
        help="在 verify_gold_shards_vs_monolithic.py 失败时仍执行步骤 3/4（旧行为；默认关闭，失败则跳过 3/4）",
    )
    ap.add_argument("--json-report", type=Path, default=None, help="写入汇总 JSON")
    args = ap.parse_args()

    repo = args.repo_root
    tasks_root = repo / "tasks"
    if not tasks_root.is_dir():
        print(f"tasks not found: {tasks_root}", file=sys.stderr)
        return 1

    names = list_tasks(tasks_root)
    skipped_gold: list[dict] = []

    if args.task_ids.strip():
        want = {x.strip() for x in args.task_ids.split(",") if x.strip()}
        slice_ = [n for n in names if n in want]
        missing = sorted(want - set(slice_))
        if missing:
            print(f"[warn] task-ids not in eligible list: {missing}", flush=True)
    else:
        slice_ = names[args.start :]
        if args.limit:
            slice_ = slice_[: args.limit]

    if args.list_only:
        for tid in slice_:
            print(tid)
        if args.json_report:
            args.json_report.write_text(json.dumps({"queue": slice_}, indent=2))
        return 0

    args.output_path.mkdir(parents=True, exist_ok=True)
    report: list[dict] = []

    for task_id in slice_:
        task_dir = tasks_root / task_id
        entry: dict = {"task_id": task_id}

        print(f"\n=== Step 1 — gold_patches 检查: {task_id} ===", flush=True)
        gold_ok, gold_reason = gold_patches_nonempty(task_dir)
        entry["gold_patches_ok"] = gold_ok
        entry["gold_patches_reason"] = gold_reason
        if gold_ok:
            print(f"  OK: gold_patches 分片齐全（0 字节分片须在 .lhb_split_noop.json  noop 列表中）", flush=True)
        else:
            print(f"  需补全: {gold_reason}", flush=True)

        print(f"\n=== Step 2 — DAG 拆分总 patch 写入 gold_patches/ ===", flush=True)
        if gold_ok:
            print(
                "  分片已齐且无非预期空文件：跳过写拆分；若需核对顺序与 unit_dag，可运行 "
                "`python3 scripts/validate_per_pr.py --task-dir tasks/<id>`。",
                flush=True,
            )
        else:
            sp = subprocess.run(
                [sys.executable, str(repo / "scripts" / "split_gold_patch_by_dag.py"), "--task-dir", str(task_dir)],
                cwd=repo,
            )
            entry["split_gold_exit"] = sp.returncode
            if sp.returncode != 0:
                print("  split_gold_patch_by_dag.py 失败；请根据 JSON 报错修正任务元数据或总 patch。", flush=True)
            else:
                gold_ok2, gold_reason2 = gold_patches_nonempty(task_dir)
                entry["gold_patches_ok_after_split"] = gold_ok2
                entry["gold_patches_reason_after_split"] = gold_reason2
                if gold_ok2:
                    print(f"  切分后体检通过。", flush=True)
                else:
                    print(f"  切分后仍不完整: {gold_reason2}", flush=True)

        # 步骤 2.1：整包 gold 与按顺序分片严格等价（技能 lhb-nonseg-oracle-dag-ftp；不调用自动化补全）
        mono = task_dir / "gold-patch.diff"
        if not mono.is_file():
            mono = task_dir / "gold_patch.diff"
        gp_dir = task_dir / "gold_patches"
        verify_exit: int | None = None
        if mono.is_file() and gp_dir.is_dir() and any(gp_dir.glob("*.diff")):
            print(f"\n=== Step 2.1 — verify_gold_shards_vs_monolithic: {task_id} ===", flush=True)
            ve = subprocess.run(
                [
                    sys.executable,
                    str(repo / "scripts" / "verify_gold_shards_vs_monolithic.py"),
                    "--task-dir",
                    str(task_dir),
                ],
                cwd=repo,
            )
            verify_exit = int(ve.returncode)
            entry["verify_shards_exit"] = verify_exit
            if verify_exit != 0:
                print(
                    "  未通过：按技能须由 Agent 完成 2.2（参照整包 gold 与 base 生成分段 patch）、写回 gold_patches 后再跑本脚本。"
                    " 默认将跳过步骤 3/4；若需仍跑 Oracle/FTP 收集日志，请加 --continue-after-shard-verify-fail。",
                    flush=True,
                )
        else:
            entry["verify_shards_exit"] = None

        shard_verify_ok = verify_exit in (None, 0)

        gold_effective = gold_ok
        if not gold_ok and entry.get("gold_patches_ok_after_split") is True:
            gold_effective = True

        if not gold_effective:
            if args.skip_empty_gold_patches:
                print("  [--skip-empty-gold-patches] 已弃用：不完整时跳过 3/4 现为默认。", flush=True)
            if not args.continue_with_incomplete_gold_patches:
                skip_reason = entry.get("gold_patches_reason_after_split") or gold_reason
                print(f"  默认：gold_patches 不完整，跳过步骤 3/4（{skip_reason}）。", flush=True)
                entry["skipped_incomplete_gold_patches"] = True
                skipped_gold.append({"task_id": task_id, "skip_reason": skip_reason})
                report.append(entry)
                continue

        if not shard_verify_ok:
            if not args.continue_after_shard_verify_fail:
                print("  默认：分片与 monolithic 严格等价校验未通过，跳过步骤 3/4。", flush=True)
                entry["skipped_after_shard_verify"] = True
                report.append(entry)
                continue

        run_id = f"ns_{task_id}"
        run_dir = args.output_path / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        results_json = run_dir / "results.json"

        print(f"\n=== Step 3 — Oracle: {task_id} ===", flush=True)
        ora_cmd, ora_env = _oracle_cmd_env(repo, task_id, run_id, args.output_path)
        r = subprocess.run(ora_cmd, cwd=repo, env=ora_env)
        ok = oracle_resolved(results_json)
        entry["lhb_exit"] = r.returncode
        entry["oracle_resolved"] = ok
        print(f"  Oracle 结束: exit={r.returncode}, is_resolved={ok}", flush=True)

        if args.skip_ftp:
            report.append(entry)
            continue

        print(f"\n=== Step 4 — validate_per_pr strict FTP: {task_id} ===", flush=True)
        ftp_env = {**os.environ}
        pp = str(repo)
        prev = ftp_env.get("PYTHONPATH", "")
        if prev:
            pp = pp + os.pathsep + prev
        ftp_env["PYTHONPATH"] = pp
        v = subprocess.run(
            [
                sys.executable,
                str(repo / "scripts" / "validate_per_pr.py"),
                "--task-dir",
                str(task_dir),
                "--strict-fail-to-pass",
            ],
            cwd=repo,
            env=ftp_env,
        )
        entry["validate_exit"] = v.returncode
        entry["ftp_ok"] = v.returncode == 0
        report.append(entry)

    if args.json_report:
        out = {"skipped_empty_gold_queue": skipped_gold, "runs": report}
        args.json_report.write_text(json.dumps(out, indent=2))
        print(f"\nWrote {args.json_report}", flush=True)

    # 非零退出：本批任一 strict FTP 失败（已执行步骤 4 时）
    bad = any(row.get("ftp_ok") is False for row in report)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
