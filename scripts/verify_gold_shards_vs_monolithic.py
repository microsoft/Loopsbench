#!/usr/bin/env python3
"""
Verify that applying gold_patches/*.diff in harness order is **strictly equivalent**
to applying the monolithic gold patch (same tree as bytes under the task ``base/`` copy).

All patching happens under a tempfile; the task ``base/`` directory is never modified.

Shard order (first match wins):
  1. ``get_pr_order(task_dir)`` from ``validate_per_pr.py`` (same as solution.sh / DAG logic)
  2. If that is ``['gold']`` or unusable: ``_get_topological_pr_order(task_dir)``
  3. Else: sorted stems of ``gold_patches/*.diff``

Strip level for ``patch`` (``-pN``) on the workspace root:
  By default, try ``N = 0..5`` with ``patch --dry-run`` on a fresh ``base/`` copy until one applies.

Optional overrides: ``--strip``, ``--shard-order`` (comma-separated stems).

Exit codes: 0 OK, 1 usage/config error, 2 mismatch or patch failure, 3 could not auto-detect strip.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_validate_per_pr():
    path = _repo_root() / "scripts" / "validate_per_pr.py"
    spec = importlib.util.spec_from_file_location("lhb_validate_per_pr", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _find_monolithic(task_dir: Path) -> Path | None:
    for name in ("gold-patch.diff", "gold_patch.diff"):
        p = task_dir / name
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def _tree_hashes(root: Path) -> dict[str, str]:
    """relposix -> sha256 hex; skips __pycache__ and .git."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if "__pycache__" in p.parts or ".git" in p.parts:
            continue
        rel = p.relative_to(root).as_posix()
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        out[rel] = h
    return out


def _copy_base(base_dir: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for child in base_dir.iterdir():
        target = dest / child.name
        if child.is_dir():
            shutil.copytree(child, target, symlinks=True)
        else:
            shutil.copy2(child, target)


def _synthetic_base_from_task_root(task_dir: Path, dest: Path) -> None:
    """task_zjuos-style layout: Lab*/docs/requirements live under task_dir (no base/)."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name in ("Lab0", "Lab1", "Lab2", "Lab3", "docs", "requirements"):
        p = task_dir / name
        if p.is_dir():
            shutil.copytree(p, dest / name, symlinks=True)


def _inner_patch_subdir(base_dir: Path, monolithic: Path) -> str | None:
    """If monolithic uses inner-repo paths (e.g. ``a/src/...``) under ``base/NJU_DBPractice``, return that subdir name."""
    inner = base_dir / "NJU_DBPractice"
    if not inner.is_dir() or not (inner / "src").is_dir():
        return None
    head = monolithic.read_bytes()[:32768]
    if b"a/NJU_DBPractice/" in head or b"b/NJU_DBPractice/" in head:
        return None
    if b"diff --git a/src/" in head or b"diff --git b/src/" in head:
        return "NJU_DBPractice"
    return None


def _patch_cwd(ws_root: Path, inner_subdir: str | None) -> Path:
    return ws_root / inner_subdir if inner_subdir else ws_root


def _detect_strip(
    base_dir: Path, monolithic: Path, tmp: Path, max_p: int = 5, *, inner_subdir: str | None = None
) -> int | None:
    blob = monolithic.read_bytes()
    for p in range(0, max_p + 1):
        ws = tmp / f"strip_try_{p}"
        _copy_base(base_dir, ws)
        cwd = _patch_cwd(ws, inner_subdir)
        if inner_subdir and not cwd.is_dir():
            continue
        r = subprocess.run(
            ["patch", f"-p{p}", "--dry-run", "--no-backup-if-mismatch", "-d", str(cwd)],
            input=blob,
            capture_output=True,
        )
        if r.returncode == 0:
            return p
    return None


def _apply_patch(ws_root: Path, blob: bytes, strip: int, *, inner_subdir: str | None = None) -> subprocess.CompletedProcess:
    cwd = _patch_cwd(ws_root, inner_subdir)
    return subprocess.run(
        ["patch", f"-p{strip}", "--no-backup-if-mismatch", "-d", str(cwd)],
        input=blob,
        capture_output=True,
    )


def _shard_order(task_dir: Path, gold_dir: Path, vp, override: list[str] | None) -> list[str]:
    if override is not None:
        return list(override)
    stems = {p.stem.removeprefix("pr_") for p in gold_dir.glob("*.diff")}
    if not stems:
        return []

    raw: list[str] = []
    try:
        raw = list(vp.get_pr_order(task_dir))
    except Exception:
        raw = []

    def _filter_existing(order: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for u in order:
            u = str(u).strip()
            if not u or u == "gold" or u in seen:
                continue
            if (gold_dir / f"{u}.diff").is_file():
                out.append(u)
                seen.add(u)
        return out

    cand = _filter_existing(raw)
    if cand and stems <= set(cand):
        return [u for u in cand if u in stems]

    topo = []
    try:
        topo = list(vp._get_topological_pr_order(task_dir))
    except Exception:
        topo = []
    cand2 = _filter_existing(topo)
    if cand2 and stems <= set(cand2):
        return [u for u in cand2 if u in stems]

    return sorted(stems)


def verify(
    task_dir: Path,
    *,
    strip: int | None,
    shard_order: list[str] | None,
    max_strip: int,
) -> tuple[bool, dict]:
    task_dir = task_dir.resolve()
    explicit_base = task_dir / "base"
    gold_dir = task_dir / "gold_patches"
    monolithic = _find_monolithic(task_dir)
    report: dict = {"task_dir": str(task_dir), "ok": False}

    use_synthetic_base = not explicit_base.is_dir() and (task_dir / "Lab1").is_dir() and (task_dir / "Lab3").is_dir()
    if not explicit_base.is_dir() and not use_synthetic_base:
        report["error"] = f"missing base dir: {explicit_base}"
        return False, report
    if monolithic is None:
        report["error"] = "no non-empty gold-patch.diff or gold_patch.diff"
        return False, report
    if not gold_dir.is_dir():
        report["error"] = f"missing {gold_dir}"
        return False, report

    vp = _load_validate_per_pr()
    order = _shard_order(task_dir, gold_dir, vp, shard_order)
    report["shard_order"] = order
    if not order:
        report["error"] = "no shard stems (gold_patches/*.diff)"
        return False, report

    report["monolithic"] = str(monolithic)

    with tempfile.TemporaryDirectory(prefix="lhb_verify_shards_") as td_s:
        td = Path(td_s)
        if explicit_base.is_dir():
            base_dir = explicit_base
        else:
            base_dir = td / "synthetic_base"
            _synthetic_base_from_task_root(task_dir, base_dir)
            report["synthetic_base"] = True

        inner_subdir = _inner_patch_subdir(base_dir, monolithic)
        if inner_subdir:
            report["patch_inner_subdir"] = inner_subdir

        if strip is None:
            p = _detect_strip(base_dir, monolithic, td, max_p=max_strip, inner_subdir=inner_subdir)
            if p is None:
                report["error"] = (
                    f"could not auto-detect patch strip (0..{max_strip}); "
                    "retry with --strip N or check monolithic paths vs base/ layout"
                )
                return False, report
            strip = p
        report["strip"] = strip

        ws_mono = td / "workspace_mono"
        ws_shard = td / "workspace_shards"
        _copy_base(base_dir, ws_mono)
        _copy_base(base_dir, ws_shard)

        r0 = _apply_patch(ws_mono, monolithic.read_bytes(), strip, inner_subdir=inner_subdir)
        if r0.returncode != 0:
            report["error"] = "monolithic patch apply failed"
            report["monolithic_stderr"] = r0.stderr.decode(errors="replace")
            return False, report

        for stem in order:
            p = gold_dir / f"{stem}.diff"
            if not p.is_file() or p.stat().st_size == 0:
                continue
            r = _apply_patch(ws_shard, p.read_bytes(), strip, inner_subdir=inner_subdir)
            if r.returncode != 0:
                report["error"] = f"shard apply failed: {p.name}"
                report["shard"] = p.name
                report["shard_stderr"] = r.stderr.decode(errors="replace")
                return False, report

        h_mono = _tree_hashes(ws_mono)
        h_shard = _tree_hashes(ws_shard)
        report["file_count_mono"] = len(h_mono)
        report["file_count_shard"] = len(h_shard)

        if h_mono != h_shard:
            only_m = sorted(set(h_mono) - set(h_shard))
            only_s = sorted(set(h_shard) - set(h_mono))
            diff_h = sorted(k for k in h_mono if k in h_shard and h_mono[k] != h_shard[k])
            report["error"] = "tree fingerprint mismatch after mono vs sequential shards"
            report["only_in_monolithic"] = only_m[:200]
            report["only_in_sharded"] = only_s[:200]
            report["hash_diff_paths"] = diff_h[:200]
            return False, report

    report["ok"] = True
    report["message"] = (
        f"OK: strip=-p{strip}, {len(order)} shard(s), {len(h_mono)} files under base/ match byte-for-byte (sha256-per-file)"
    )
    return True, report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--task-dir", type=Path, required=True, help="tasks/task_foo")
    ap.add_argument(
        "--strip",
        type=int,
        default=None,
        help="patch strip level (-pN); default auto 0..--max-strip",
    )
    ap.add_argument(
        "--max-strip",
        type=int,
        default=5,
        help="max strip level when auto-detecting (default 5)",
    )
    ap.add_argument(
        "--shard-order",
        type=str,
        default=None,
        help="override shard order: comma-separated stems (e.g. pr_1,pr_2)",
    )
    ap.add_argument("--json-out", type=Path, default=None, help="write JSON report to this path")
    args = ap.parse_args()

    shard_order = None
    if args.shard_order:
        shard_order = [s.strip() for s in args.shard_order.split(",") if s.strip()]

    ok, report = verify(
        args.task_dir,
        strip=args.strip,
        shard_order=shard_order,
        max_strip=args.max_strip,
    )
    text = json.dumps(report, indent=2) + "\n"
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text, encoding="utf-8")
    if ok:
        print(report.get("message", "OK"))
        if args.json_out:
            print(f"wrote {args.json_out}")
        return 0
    print(text, file=sys.stderr)
    err = report.get("error") or ""
    if "could not auto-detect patch strip" in err:
        return 3
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
