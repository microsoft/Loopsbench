#!/usr/bin/env python3
"""
validate_per_pr.py — Per-PR cumulative validation for seg tasks.

For a given seg task, this script can strictly validate that for every tested PR:
1. Docker builds successfully.
2. The task container starts successfully.
3. Earlier PR patches replay in oracle order.
4. The current PR's tests fail before its patch is applied.
5. The current PR's patch applies successfully.
6. The same tests pass after the patch is applied.

Usage:
    python3 scripts/validate_per_pr.py <repo_id> --segment 1
    python3 scripts/validate_per_pr.py <repo_id> --segment 1 --strict-fail-to-pass
    python3 scripts/validate_per_pr.py --task-dir tasks/task_django_seg01 --strict-fail-to-pass
"""

import argparse
import ast
import difflib
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path

import yaml


def _django_run_is_only_skips(run_result: dict | None) -> bool:
    if not run_result or run_result.get("returncode") != 0:
        return False
    stdout = run_result.get("stdout", "") or ""
    if "Testing against Django installed" not in stdout:
        return False
    skipped_matches = [int(m.group(1)) for m in re.finditer(r"skipped=(\d+)", stdout)]
    if skipped_matches and max(skipped_matches) > 0:
        return True
    return bool(re.search(r"^s+$", stdout, re.MULTILINE))


def _django_run_is_backend_gated(run_result: dict | None) -> bool:
    if not run_result:
        return False
    stdout = run_result.get("stdout", "") or ""
    if "Testing against Django installed" not in stdout:
        return False
    return "A GIS database backend is required to run gis_tests." in stdout


def _django_module_label_for_path(path: str) -> str | None:
    if not path.endswith(".py"):
        return None
    module = path.removeprefix("tests/") if path.startswith("tests/") else path
    return module.replace("/", ".").removesuffix(".py")


def _base_name(base: ast.expr) -> str | None:
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    if isinstance(base, ast.Subscript):
        return _base_name(base.value)
    if isinstance(base, ast.Call):
        return _base_name(base.func)
    return None


def _resolve_relative_py_import(current_path: Path, module: str | None, level: int) -> Path | None:
    if level <= 0:
        return None

    def resolve_from(base_dir: Path) -> Path | None:
        for _ in range(level - 1):
            base_dir = base_dir.parent
        if module:
            candidate = base_dir.joinpath(*module.split("."))
        else:
            candidate = base_dir
        if candidate.is_dir():
            candidate = candidate / "__init__.py"
        else:
            candidate = candidate.with_suffix(".py")
        return candidate if candidate.is_file() else None

    candidate = resolve_from(current_path.parent)
    if candidate is not None:
        return candidate

    task_dir = next(
        (parent for parent in current_path.parents if parent.name.startswith("task_") and parent.parent.name == "tasks"),
        None,
    )
    if task_dir is None:
        return None
    try:
        rel_to_task = current_path.relative_to(task_dir)
    except ValueError:
        return None
    if len(rel_to_task.parts) < 3 or rel_to_task.parts[0] != "tests" or not rel_to_task.parts[1].isdigit():
        return None

    pr_num = rel_to_task.parts[1]
    repo_rel = Path(*rel_to_task.parts[2:])
    repo_base_dir = repo_rel.parent
    for _ in range(level - 1):
        repo_base_dir = repo_base_dir.parent
    if module:
        candidate_rel = repo_base_dir.joinpath(*module.split("."))
    else:
        candidate_rel = repo_base_dir
    if candidate_rel.suffix != ".py":
        candidate_rel = candidate_rel.with_suffix(".py")
    candidate_rel_str = candidate_rel.as_posix()
    return _resolve_test_source_path(task_dir, pr_num, candidate_rel_str) or _resolve_previous_django_test_source_path(task_dir, pr_num, candidate_rel_str)


@lru_cache(maxsize=None)
def _parse_python_module(py_path: Path) -> ast.Module | None:
    try:
        return ast.parse(py_path.read_text())
    except Exception:
        return None


@lru_cache(maxsize=None)
def _get_module_class_bases(py_path: Path) -> dict[str, list[str]]:
    tree = _parse_python_module(py_path)
    if tree is None:
        return {}

    imported_names: dict[str, tuple[Path | None, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            imported_path = None
            if node.level:
                imported_path = _resolve_relative_py_import(py_path, node.module, node.level)
            for alias in node.names:
                if alias.name == "*":
                    continue
                imported_names[alias.asname or alias.name] = (imported_path, alias.name)

    class_bases: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases: list[str] = []
            for base in node.bases:
                base_name = _base_name(base)
                if base_name:
                    bases.append(base_name)
            class_bases[node.name] = bases

    resolved: dict[str, list[str]] = {}
    for class_name, bases in class_bases.items():
        resolved_bases: list[str] = []
        for base_name in bases:
            imported_path, imported_name = imported_names.get(base_name, (None, base_name))
            if imported_path is not None:
                resolved_bases.append(f"{imported_path}::{imported_name}")
            else:
                resolved_bases.append(base_name)
        resolved[class_name] = resolved_bases
    return resolved


def _class_inherits_django_test_case(py_path: Path, class_name: str, seen: set[tuple[Path, str]] | None = None) -> bool:
    key = (py_path, class_name)
    if seen is None:
        seen = set()
    if key in seen:
        return False
    seen.add(key)

    test_case_bases = {
        "SimpleTestCase",
        "TestCase",
        "TransactionTestCase",
        "LiveServerTestCase",
        "StaticLiveServerTestCase",
    }
    for base_name in _get_module_class_bases(py_path).get(class_name, []):
        if base_name in test_case_bases or base_name.endswith("TestCase"):
            return True
        if "::" in base_name:
            imported_path_str, imported_class = base_name.split("::", 1)
            imported_path = Path(imported_path_str)
            if _class_inherits_django_test_case(imported_path, imported_class, seen):
                return True
        elif _class_inherits_django_test_case(py_path, base_name, seen):
            return True
    return False


def _collect_django_test_labels(py_path: Path, module_label: str) -> list[tuple[str, int, int]]:
    tree = _parse_python_module(py_path)
    if tree is None:
        return []

    labels: list[tuple[str, int, int]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if not _class_inherits_django_test_case(py_path, node.name):
                continue
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test"):
                    start = getattr(child, "lineno", None)
                    end = getattr(child, "end_lineno", start)
                    if start is not None and end is not None:
                        labels.append((f"{module_label}.{node.name}.{child.name}", start, end))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            start = getattr(node, "lineno", None)
            end = getattr(node, "end_lineno", start)
            if start is not None and end is not None:
                labels.append((f"{module_label}.{node.name}", start, end))
    return labels


def _extract_changed_django_test_labels(base_path: Path, overlay_path: Path, module_label: str) -> list[str]:
    if not base_path.is_file() or not overlay_path.is_file():
        return []

    try:
        base_lines = base_path.read_text().splitlines()
        overlay_lines = overlay_path.read_text().splitlines()
    except Exception:
        return []

    changed_overlay_lines: set[int] = set()
    matcher = difflib.SequenceMatcher(a=base_lines, b=overlay_lines)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            changed_overlay_lines.update(range(j1 + 1, j2 + 1))
    if not changed_overlay_lines:
        return []

    labels: list[str] = []
    for label, start, end in _collect_django_test_labels(overlay_path, module_label):
        if any(start <= line_no <= end for line_no in changed_overlay_lines):
            labels.append(label)
    return labels


def _resolve_pr_test_source_path(task_dir: Path, pr_num: str, rel: str) -> Path | None:
    pr_dir = get_pr_test_dir(task_dir, pr_num)
    if pr_dir is None:
        return None
    candidate = pr_dir / rel
    return candidate if candidate.is_file() else None


def _resolve_test_source_path(task_dir: Path, pr_num: str, rel: str) -> Path | None:
    candidate = _resolve_pr_test_source_path(task_dir, pr_num, rel)
    if candidate is not None:
        return candidate
    shared_candidate = task_dir / "tests" / rel
    if shared_candidate.is_file():
        return shared_candidate
    return None


def _resolve_previous_django_test_source_path(task_dir: Path, pr_num: str, rel: str) -> Path:
    pr_order = get_pr_order(task_dir)
    try:
        pr_index = pr_order.index(pr_num)
    except ValueError:
        return task_dir / "base" / rel

    for previous_pr in reversed(pr_order[:pr_index]):
        candidate = _resolve_pr_test_source_path(task_dir, previous_pr, rel)
        if candidate is not None:
            return candidate
    return task_dir / "base" / rel


def _get_explicit_selected_tests(task_dir: Path, pr_num: str) -> list[str] | None:
    pr_dir = get_pr_test_dir(task_dir, pr_num)
    if pr_dir is None:
        return None
    selected_tests_path = pr_dir / "selected_tests.json"
    if not selected_tests_path.is_file():
        return None
    try:
        raw = selected_tests_path.read_text()
        # LHB canary header lines are # comments; json.load cannot parse them.
        body = "\n".join(
            line for line in raw.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        selected_tests = json.loads(body)
    except Exception:
        return None
    if not isinstance(selected_tests, list) or not all(isinstance(item, str) for item in selected_tests):
        return None
    return selected_tests


def _get_explicit_django_test_labels(task_dir: Path, pr_num: str) -> list[str] | None:
    selected_tests = _get_explicit_selected_tests(task_dir, pr_num)
    if selected_tests is None:
        return None

    labels: list[str] = []
    for item in selected_tests:
        if "::" in item:
            path, *parts = item.split("::")
            module_label = _django_module_label_for_path(path)
            if module_label is None:
                continue
            labels.append(".".join([module_label, *parts]))
        else:
            labels.append(item)
    return labels


def _django_test_path_rels_from_pr_files(test_files: list[str]) -> list[str]:
    """`.py` paths under the PR test tree for Django label discovery (not `select_test_entry_files`, which returns bool)."""
    seen: set[str] = set()
    ordered: list[str] = []
    for f in test_files:
        path = f.split("::", 1)[0]
        if not path.endswith(".py"):
            continue
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def get_django_test_labels(task_dir: Path, pr_num: str, test_files: list[str]) -> list[str]:
    explicit_labels = _get_explicit_django_test_labels(task_dir, pr_num)
    if explicit_labels is not None:
        return explicit_labels

    labels: list[str] = []
    seen: set[str] = set()
    for rel in _django_test_path_rels_from_pr_files(test_files):
        module_label = _django_module_label_for_path(rel)
        if module_label is None:
            continue
        overlay_path = _resolve_test_source_path(task_dir, pr_num, rel)
        previous_path = _resolve_previous_django_test_source_path(task_dir, pr_num, rel)
        candidates = _extract_changed_django_test_labels(previous_path, overlay_path, module_label) if overlay_path is not None else []
        if not candidates:
            candidates = [module_label]
        for label in candidates:
            if label not in seen:
                labels.append(label)
                seen.add(label)
    return labels

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent
for p in (ROOT, SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from docker_rootful import docker_cmd, docker_env
from long_horizon_bench.test_env.repo_profiles import (
    build_native_test_cmd,
    classify_test_failure,
    infer_language,
    infer_repo_id,
    infer_repo_root,
    select_test_entry_files,
)

TASKS = ROOT / "tasks"
DEFAULT_HOST_TMP_ROOT = (ROOT / "tmp").resolve()
HOST_TMP_ROOT = Path(os.environ.get("TMPDIR", str(DEFAULT_HOST_TMP_ROOT))).expanduser().resolve()
if not str(HOST_TMP_ROOT).startswith("/Data2/"):
    HOST_TMP_ROOT = DEFAULT_HOST_TMP_ROOT
HOST_LOGS_PATH = HOST_TMP_ROOT / "lhb-logs"
HOST_AGENT_LOGS_PATH = HOST_TMP_ROOT / "lhb-agent-logs"


def infer_patch_apply_cwd(task_dir: Path, repo_root: str) -> str:
    """``cd`` target for ``git apply`` / ``patch`` when diffs are relative to a subproject root.

    Shards often use ``diff --git a/src/...``; apply cwd must match where ``src/`` lives in the
    container (e.g. ``/workspace/NJU_DBPractice`` or ``/workspace/lab_db_storage``), not only
    ``repo_root`` when the lab tree is nested.
    """
    rr = repo_root.rstrip("/") or repo_root

    inner_nju = task_dir / "base" / "NJU_DBPractice"
    if inner_nju.is_dir() and (inner_nju / "src").is_dir():
        for name in ("gold-patch.diff", "gold_patch.diff"):
            mono = task_dir / name
            if mono.is_file() and mono.stat().st_size > 0:
                head = mono.read_bytes()[:65536]
                if b"a/NJU_DBPractice/" in head or b"b/NJU_DBPractice/" in head:
                    return repo_root
                if b"diff --git a/src/" in head or b"diff --git b/src/" in head:
                    return f"{rr}/NJU_DBPractice"
                break
        return repo_root

    inner_lab = task_dir / "base" / "lab_db_storage"
    if inner_lab.is_dir() and (inner_lab / "src").is_dir():
        for name in ("gold-patch.diff", "gold_patch.diff"):
            mono = task_dir / name
            if mono.is_file() and mono.stat().st_size > 0:
                head = mono.read_bytes()[:65536]
                if b"a/lab_db_storage/" in head or b"b/lab_db_storage/" in head:
                    return repo_root
                if b"diff --git a/src/" in head or b"diff --git b/src/" in head:
                    return f"{rr}/lab_db_storage"
                break
        gp = task_dir / "gold_patches"
        if gp.is_dir():
            for p in sorted(gp.glob("*.diff")):
                if p.name.startswith(".") or not p.is_file() or p.stat().st_size == 0:
                    continue
                h = p.read_bytes()[:8192]
                if b"diff --git a/src/" in h or b"diff --git b/src/" in h:
                    return f"{rr}/lab_db_storage"
                break
        return repo_root

    return repo_root


def get_task_test_timeout(task_dir: Path, cli_timeout: int | None) -> int:
    if cli_timeout is not None:
        return cli_timeout
    task_yaml = task_dir / "task.yaml"
    if task_yaml.exists():
        try:
            data = yaml.safe_load(task_yaml.read_text()) or {}
            timeout = int(data.get("max_test_timeout_sec", 0) or 0)
            if timeout > 0:
                return timeout
        except Exception:
            pass
    return 300


RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
CYAN = "\033[0;36m"
BOLD = "\033[1m"
NC = "\033[0m"

def _c(colour, text):
    return f"{colour}{text}{NC}"


def _tail(text: str, limit: int = 800) -> str:
    text = (text or "").strip()
    return text[-limit:] if len(text) > limit else text


def extract_repo_id(task_name: str) -> str:
    m = re.match(r"task_(.+)_seg\d+", task_name)
    return m.group(1) if m else ""


def extract_segment_id(task_name: str) -> int | None:
    m = re.match(r"task_.+_seg(\d+)", task_name)
    return int(m.group(1)) if m else None


def classify_command_failure(run_result: dict | None) -> tuple[str | None, str | None]:
    if not run_result or run_result.get("passed"):
        return None, None
    return classify_test_failure(
        run_result.get("stdout", ""),
        run_result.get("stderr", ""),
        run_result.get("command"),
    )


def attach_failure_classification(result: dict, run_result: dict | None) -> None:
    category, detail = classify_command_failure(run_result)
    result["failure_category"] = category
    result["failure_detail"] = detail


def find_task_dir(repo_id: str, segment: int) -> Path:
    task_dir = TASKS / f"task_{repo_id}_seg{segment:02d}"
    if not task_dir.exists():
        print(f"Task directory not found: {task_dir}")
        sys.exit(1)
    return task_dir


def _load_unit_dag(task_dir: Path) -> dict | None:
    dag_path = task_dir / "unit_dag.json"
    if not dag_path.is_file():
        return None
    try:
        dag = json.loads(dag_path.read_text())
    except Exception:
        return None
    return dag if isinstance(dag, dict) else None


def _uses_pr_test_task_runner_path(task_dir: Path, lang: str) -> bool:
    """Use ``pytest /tests/test_task_runner.py`` + ``UNIT_TEST_DIR`` for per-PR checks.

    C++ tasks use ``parser_name: native`` → ``lang == "cpp"``. The NJU DB storage / index lab
    uses ``parser_name: pytest`` (``lang == "python"``) but the same driver; ``repo_id`` in
    ``unit_dag.json`` may be renamed away from ``njudb``. We then match on ``base/lab_db_storage``
    layout, an explicit dag field, or legacy ``repo_id`` ``njudb``.
    """
    if not (task_dir / "tests" / "test_task_runner.py").is_file():
        return False
    if lang == "cpp":
        return True
    inner_lab = task_dir / "base" / "lab_db_storage"
    if inner_lab.is_dir() and (inner_lab / "src").is_dir():
        return True
    dag = _load_unit_dag(task_dir)
    if not dag:
        return False
    if dag.get("validate_per_pr_pr_tests_driver") == "test_task_runner":
        return True
    rid = dag.get("repo_id")
    if isinstance(rid, str) and rid.strip() == "njudb":
        return True
    return False


def _get_solution_metadata_pr_order(task_dir: Path) -> list[str]:
    gold_dir = task_dir / "gold_patches"
    if not gold_dir.is_dir():
        return []

    ordered: list[str] = []
    seen: set[str] = set()

    patch_order_path = task_dir / "patch_order.json"
    if patch_order_path.is_file():
        try:
            patch_order = json.loads(patch_order_path.read_text())
        except Exception:
            patch_order = None
        if isinstance(patch_order, list):
            for unit in patch_order:
                unit_id = str(unit).strip()
                if not unit_id or unit_id in seen:
                    continue
                diff_path = gold_dir / f"{unit_id}.diff"
                if diff_path.exists():
                    ordered.append(unit_id)
                    seen.add(unit_id)

    slug_map_path = task_dir / "slug_diff_map.json"
    if slug_map_path.is_file():
        try:
            slug_map = json.loads(slug_map_path.read_text())
        except Exception:
            slug_map = None
        if isinstance(slug_map, dict):
            for diff_rel in slug_map.values():
                diff_path = task_dir / str(diff_rel)
                unit_id = diff_path.stem.removeprefix("pr_")
                if diff_path.exists() and unit_id not in seen:
                    ordered.append(unit_id)
                    seen.add(unit_id)

    dag = _load_unit_dag(task_dir)
    if dag is not None:
        nodes = dag.get("nodes")
        if isinstance(nodes, list):
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                unit_id = str(node.get("id", "")).strip()
                if not unit_id or unit_id in seen:
                    continue
                diff_path = gold_dir / f"{unit_id}.diff"
                if diff_path.exists():
                    ordered.append(unit_id)
                    seen.add(unit_id)

    for diff_path in sorted(gold_dir.glob("*.diff")):
        unit_id = diff_path.stem.removeprefix("pr_")
        if unit_id not in seen:
            ordered.append(unit_id)
            seen.add(unit_id)

    return ordered


def _patch_id_sort_tail(patch_id: str) -> tuple[int, int | str]:
    """Stable tie-break: numeric PR ids (seg) vs string unit ids (non-seg DAG tasks)."""
    try:
        return (0, int(patch_id))
    except ValueError:
        return (1, patch_id)


def _get_topological_pr_order(task_dir: Path) -> list[str]:
    gold_dir = task_dir / "gold_patches"
    if not gold_dir.is_dir():
        return []

    patch_ids = {path.stem.removeprefix("pr_") for path in gold_dir.glob("*.diff")}
    if not patch_ids:
        return []

    dag = _load_unit_dag(task_dir)
    if dag is None:
        return []

    nodes = dag.get("nodes")
    edges = dag.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return []

    slug_order: dict[str, int] = {}
    slug_map_path = task_dir / "slug_diff_map.json"
    if slug_map_path.is_file():
        try:
            slug_map = json.loads(slug_map_path.read_text())
        except Exception:
            slug_map = None
        if isinstance(slug_map, dict):
            for index, diff_rel in enumerate(slug_map.values()):
                unit_id = (task_dir / str(diff_rel)).stem.removeprefix("pr_")
                if unit_id in patch_ids and unit_id not in slug_order:
                    slug_order[unit_id] = index

    indegree = {patch_id: 0 for patch_id in patch_ids}
    adjacency: dict[str, list[str]] = {patch_id: [] for patch_id in patch_ids}
    node_order: dict[str, int] = {}

    for index, node in enumerate(nodes):
        if isinstance(node, dict):
            node_id = str(node.get("id", ""))
            if node_id in patch_ids:
                node_order[node_id] = index

    for edge in edges:
        if not isinstance(edge, dict):
            continue
        src = str(edge.get("from", ""))
        dst = str(edge.get("to", ""))
        if src in patch_ids and dst in patch_ids:
            adjacency[src].append(dst)
            indegree[dst] += 1

    ready = sorted(
        (patch_id for patch_id, degree in indegree.items() if degree == 0),
        key=lambda patch_id: (
            node_order.get(patch_id, slug_order.get(patch_id, float("inf"))),
            slug_order.get(patch_id, float("inf")),
            _patch_id_sort_tail(patch_id),
        ),
    )
    ordered: list[str] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        newly_ready: list[str] = []
        for neighbor in adjacency[current]:
            indegree[neighbor] -= 1
            if indegree[neighbor] == 0:
                newly_ready.append(neighbor)
        if newly_ready:
            ready = sorted(
                ready + newly_ready,
                key=lambda patch_id: (
                    node_order.get(patch_id, slug_order.get(patch_id, float("inf"))),
                    slug_order.get(patch_id, float("inf")),
                    _patch_id_sort_tail(patch_id),
                ),
            )

    if len(ordered) == len(patch_ids):
        return ordered
    return []


def _strict_after_patch_chain(task_dir: Path, pr_num: str) -> list[str]:
    """Ordered unit ids to apply for strict FTP *after* the before-patch test run.

    Workspace is reset to hollow base; replay patches in solution order **through**
    ``pr_num`` inclusive so the ``after`` state matches cumulative oracle apply for
    this PR (not the full task unless ``pr_num`` is last).

    fdcompiler: each ``tests/<unit>/test_expanded.py`` drives ``LLVMCompiler`` end-to-end
    (llvm-link / lli). Intermediate patch prefixes often emit IR that breaks llvm-link for
    those scenarios even when the final oracle tree is correct. For strict *after*, apply
    the full ``solution.sh`` patch order so *after* matches the cumulative gold state; *before*
    remains hollow base so fail-to-pass still distinguishes unimplemented from implemented.

    hgddatabase: expanded tests invoke the built ``QueryProcessing`` / ``QueryOptimize`` binaries
    against shared data; partial patch stacks leave operators or I/O paths inconsistent with
    what the suite expects after a full oracle apply. Same tradeoff as fdcompiler: strict
    *after* replays the full ``solution.sh`` order (rebuild happens in ``build_native_test_cmd``).

    machinelearning: the only per-PR test dir (``a3_usl_clustering``) bundles checks for A1-SL
    trainers, A3 clustering, and MDP helpers in one suite; a prefix-only patch stack leaves later
    modules hollow while the tests assume full-oracle behaviour. Strict *after* uses the full
    ``solution.sh`` order like fdcompiler.

    NJU DB storage/index lab: ``run-tests.sh`` can scope GTest binaries using ``LHB_PR_UNIT`` /
    ``LHB_STORAGE_GTEST_TARGETS`` (set from ``tests/test_task_runner.py``), so strict *after* can
    stay prefix-only through ``pr_num``. For a legacy monolithic ``run-tests.sh`` that always runs
    all binaries, set ``validate_per_pr_strict_after_full_solution`` to ``true`` in ``unit_dag.json``
    to replay the full ``solution.sh`` order on *after*.
    """
    order = get_pr_order(task_dir)
    dag = _load_unit_dag(task_dir)
    if (
        isinstance(dag, dict)
        and dag.get("validate_per_pr_strict_after_full_solution") is True
        and order
        and pr_num in order
    ):
        return list(order)
    if pr_num in order:
        i = order.index(pr_num)
        return order[: i + 1]
    topo = _get_topological_pr_order(task_dir)
    if topo and pr_num in topo:
        i = topo.index(pr_num)
        return topo[: i + 1]
    return [pr_num]


def _violates_dag_order(task_dir: Path, ordered: list[str]) -> bool:
    dag = _load_unit_dag(task_dir)
    if dag is None:
        return False
    edges = dag.get("edges")
    if not isinstance(edges, list):
        return False

    positions = {unit: index for index, unit in enumerate(ordered)}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        src = str(edge.get("from", ""))
        dst = str(edge.get("to", ""))
        if src in positions and dst in positions and positions[src] > positions[dst]:
            return True
    return False


def get_pr_order(task_dir: Path) -> list[str]:
    solution = task_dir / "solution.sh"
    units: list[str] = []
    saw_monolithic = False
    if solution.exists():
        solution_text = solution.read_text()
        for line in solution_text.splitlines():
            m = re.search(
                r"(?:gold_patches/|\$\{PATCH_ROOT\}/|\$PATCH_ROOT/)(?!\$\{)(?!\$\()([A-Za-z0-9_][A-Za-z0-9_.-]*)\.diff",
                line,
            )
            if m:
                units.append(m.group(1).removeprefix("pr_"))
        saw_monolithic = "gold-patch.diff" in solution_text
    if units:
        ordered: list[str] = []
        seen: set[str] = set()
        for unit in units:
            if unit not in seen:
                seen.add(unit)
                ordered.append(unit)
        topo_order = _get_topological_pr_order(task_dir)
        if topo_order and set(topo_order) == set(ordered) and _violates_dag_order(task_dir, ordered):
            return topo_order
        return ordered

    solution_metadata_order = _get_solution_metadata_pr_order(task_dir)
    topo_order = _get_topological_pr_order(task_dir)
    if solution_metadata_order:
        if topo_order and set(topo_order) == set(solution_metadata_order) and _violates_dag_order(task_dir, solution_metadata_order):
            return topo_order
        return solution_metadata_order

    if topo_order:
        return topo_order

    if saw_monolithic:
        return ["gold"]

    gold_dir = task_dir / "gold_patches"
    if gold_dir.exists():
        ordered = []
        for path in sorted(gold_dir.glob("*.diff")):
            ordered.append(path.stem.removeprefix("pr_"))
        if ordered:
            return ordered

    monolithic = task_dir / "gold-patch.diff"
    if monolithic.is_file():
        return ["gold"]

    print(f"Could not determine PR order for {task_dir}")
    sys.exit(1)


def _candidate_test_dirs(task_dir: Path, pr_num: str) -> list[Path]:
    tests_dir = task_dir / "tests"
    return [tests_dir / pr_num, tests_dir / f"pr_{pr_num}"]


def is_task_level_strict_task(task_dir: Path) -> bool:
    solution = task_dir / "solution.sh"
    run_tests = task_dir / "run-tests.sh"
    if not solution.is_file() or not run_tests.is_file():
        return False
    if get_pr_order(task_dir) == ["gold"]:
        return True

    repo_id = infer_repo_id(task_dir, fallback_repo_id=extract_repo_id(task_dir.name) or task_dir.name.removeprefix("task_"))
    if repo_id != "hadoop" or not (task_dir / "tests" / "test_unit_runner.py").is_file():
        return False

    return not any(
        test_dir.is_dir()
        for pr_num in get_pr_order(task_dir)
        for test_dir in _candidate_test_dirs(task_dir, pr_num)
    )


def _unit_has_explicit_tests(task_dir: Path, pr_num: str) -> bool | None:
    dag = _load_unit_dag(task_dir)
    if dag is None:
        return None
    nodes = dag.get("nodes")
    if not isinstance(nodes, list):
        return None
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if str(node.get("id", "")).strip() != pr_num:
            continue
        has_tests = node.get("has_tests")
        return has_tests if isinstance(has_tests, bool) else None
    return None


def get_tested_prs(task_dir: Path) -> set[str]:
    repo_id = infer_repo_id(task_dir, fallback_repo_id=extract_repo_id(task_dir.name) or task_dir.name.removeprefix("task_"))
    if is_task_level_strict_task(task_dir):
        tests_dir = task_dir / "tests"
        if not tests_dir.is_dir():
            return set()
        return {"gold"} if any(f.is_file() for f in tests_dir.rglob("*")) else set()

    tested = set()
    for pr_num in get_pr_order(task_dir):
        explicit_has_tests = _unit_has_explicit_tests(task_dir, pr_num)
        if explicit_has_tests is False:
            continue
        files = get_pr_test_files(task_dir, pr_num)
        if repo_id == "django":
            explicit_selected_tests = _get_explicit_selected_tests(task_dir, pr_num)
            patch_host_path = resolve_patch_host_path(task_dir, pr_num)
            patch_touched_files = get_patch_touched_files(patch_host_path) if patch_host_path else set()
            if explicit_selected_tests is None and patch_touched_files and all(path.startswith("tests/") for path in patch_touched_files):
                continue
            if get_django_test_labels(task_dir, pr_num, files):
                tested.add(pr_num)
        elif select_test_entry_files(repo_id, files):
            tested.add(pr_num)
    return tested


def get_pr_test_dir(task_dir: Path, pr_num: str) -> Path | None:
    if pr_num == "gold":
        tests_dir = task_dir / "tests"
        return tests_dir if tests_dir.is_dir() else None
    for pr_dir in _candidate_test_dirs(task_dir, pr_num):
        if pr_dir.is_dir():
            return pr_dir
    return None


def get_pr_test_files(task_dir: Path, pr_num: str) -> list[str]:
    pr_dir = get_pr_test_dir(task_dir, pr_num)
    if pr_dir is None:
        return []

    files = {str(f.relative_to(pr_dir)) for f in pr_dir.rglob("*") if f.is_file()}

    shared_tests_dir = task_dir / "tests" / "tests"
    if pr_num != "gold" and shared_tests_dir.is_dir():
        tests_root = task_dir / "tests"
        files.update(str(f.relative_to(tests_root)) for f in shared_tests_dir.rglob("*") if f.is_file())

    return sorted(files)


def resolve_patch_host_path(task_dir: Path, pr_num: str) -> Path | None:
    if pr_num == "gold":
        monolithic = task_dir / "gold-patch.diff"
        if monolithic.is_file():
            return monolithic
        monolithic_u = task_dir / "gold_patch.diff"
        return monolithic_u if monolithic_u.is_file() else None
    candidates = [
        task_dir / "gold_patches" / f"pr_{pr_num}.diff",
        task_dir / "gold_patches" / f"{pr_num}.diff",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def get_patch_touched_files(patch_path: Path | None) -> set[str]:
    if patch_path is None or not patch_path.is_file():
        return set()

    touched: set[str] = set()
    for line in patch_path.read_text(encoding="latin-1").splitlines():
        if not line.startswith("+++ "):
            continue
        path = line[4:].strip()
        if path == "/dev/null":
            continue
        if path.startswith("b/"):
            path = path[2:]
        touched.add(path)
    return touched


def _post_patch_runtime_refresh(
    container: str,
    repo_id: str,
    repo_root: str,
    touched_files: set[str],
    *,
    timeout: int = 300,
) -> dict:
    repo_q = shlex.quote(repo_root)
    refresh_cmd: list[str] | None = None

    if repo_id == "rails":
        bundle_changed = any(
            path in {"Gemfile", "Gemfile.lock"} or path.endswith(".gemspec")
            for path in touched_files
        )
        npm_changed = any(
            Path(path).name in {
                "package.json",
                "package-lock.json",
                "npm-shrinkwrap.json",
                "yarn.lock",
                "pnpm-lock.yaml",
                "bun.lock",
                "bun.lockb",
            }
            for path in touched_files
        )
        steps: list[str] = []
        if bundle_changed:
            steps.append(
                "bundle install --jobs 4 --retry 3 >/tmp/lhb_bundle_install.log 2>&1 "
                "|| { tail -80 /tmp/lhb_bundle_install.log; exit 1; }"
            )
        if npm_changed:
            steps.append(
                "npm install --no-audit --no-fund >/tmp/lhb_npm_install.log 2>&1 "
                "|| { tail -80 /tmp/lhb_npm_install.log; exit 1; }"
            )
        if steps:
            refresh_cmd = ["bash", "-c", f"set -o pipefail; cd {repo_q} && " + " && ".join(steps)]

    if refresh_cmd is None:
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": "", "command": None}

    rc, stdout, stderr = docker_exec(container, refresh_cmd, timeout=timeout)
    return {
        "ok": rc == 0,
        "returncode": rc,
        "stdout": _tail(stdout),
        "stderr": _tail(stderr),
        "command": refresh_cmd,
    }


def has_git(container: str) -> bool:
    rc, _, _ = docker_exec(container, ["bash", "-lc", "command -v git >/dev/null 2>&1"])
    return rc == 0


def ensure_workspace_dirs(container: str) -> tuple[int, str, str]:
    return docker_exec(container, ["bash", "-c", "mkdir -p /tmp/lhb_patches /tmp/lhb_test_backup /tests"], timeout=30)


def ensure_git_available(container: str, timeout: int = 300) -> tuple[bool, dict]:
    if has_git(container):
        return True, {"installed": False, "stdout": "", "stderr": ""}
    script = """
set -e
if command -v apt-get >/dev/null 2>&1; then
  apt-get update && apt-get install -y git
elif command -v apk >/dev/null 2>&1; then
  apk add --no-cache git
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y git
elif command -v microdnf >/dev/null 2>&1; then
  microdnf install -y git
elif command -v yum >/dev/null 2>&1; then
  yum install -y git
else
  echo 'no supported package manager to install git' >&2
  exit 1
fi
"""
    rc, stdout, stderr = docker_exec(container, ["bash", "-lc", script], timeout=timeout)
    ok = rc == 0 and has_git(container)
    return ok, {"installed": rc == 0, "returncode": rc, "stdout": _tail(stdout), "stderr": _tail(stderr)}


def docker_exec(container: str, cmd: list[str], timeout: int = 300) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            [*docker_cmd("exec", container), *cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=docker_env(),
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 1, "", f"timed out ({timeout}s)"
    except Exception as e:
        return 1, "", str(e)


def copy_host_path_to_container(host_path: Path, container: str, container_path: str, timeout: int = 180) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            [*docker_cmd("cp", str(host_path), f"{container}:{container_path}")],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=docker_env(),
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 1, "", f"timed out ({timeout}s)"
    except Exception as e:
        return 1, "", str(e)


def copy_tree_to_container(host_dir: Path, container: str, container_dir: str, timeout: int = 120) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            [*docker_cmd("cp", str(host_dir) + "/.", f"{container}:{container_dir}")],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=docker_env(),
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 1, "", f"timed out ({timeout}s)"
    except Exception as e:
        return 1, "", str(e)


def apply_patch_and_commit(
    container: str,
    repo_root: str,
    patch_in_container: str,
    pr_num: str,
    timeout: int = 120,
    use_git: bool = True,
    *,
    patch_cwd: str | None = None,
) -> dict:
    cwd_for_patch = patch_cwd or repo_root
    repo_root_n = repo_root.rstrip("/") or repo_root
    cwd_n = cwd_for_patch.rstrip("/") or cwd_for_patch
    # git apply from a nested cwd can mis-resolve paths like a/src/... onto the git root instead
    # of the subproject (e.g. /workspace/src vs /workspace/lab_db_storage/src). Apply from the
    # repo root with --directory=<nested> so shards match the tree under lab_db_storage/.
    if cwd_n != repo_root_n and cwd_n.startswith(repo_root_n + "/"):
        rel_nested = os.path.relpath(cwd_n, repo_root_n).replace(os.sep, "/")
        git_apply_cd_q = shlex.quote(repo_root_n)
        git_apply_extra_line = f"GIT_APPLY_EXTRA=(--directory {shlex.quote(rel_nested)})"
    else:
        git_apply_cd_q = shlex.quote(cwd_for_patch)
        git_apply_extra_line = "GIT_APPLY_EXTRA=()"
    patch_cd_q = shlex.quote(cwd_for_patch)
    repo_q = shlex.quote(repo_root)
    patch_q = shlex.quote(patch_in_container)
    commit_q = shlex.quote(f"PR #{pr_num}")
    if use_git:
        script = (
            "set -e; "
            f"{git_apply_extra_line}; "
            f"cd {git_apply_cd_q}; "
            f"if [ ! -s {patch_q} ]; then git -C {repo_q} commit -m {commit_q} --allow-empty; exit 0; fi; "
            "section_dir=$(mktemp -d /tmp/lhb_patch_sections.XXXXXX); "
            "section_count=$(SECTION_DIR=\"$section_dir\" python3 - <<'PY'\n"
            "from pathlib import Path\n"
            "import os\n"
            f"text = Path({patch_in_container!r}).read_text(encoding='latin-1')\n"
            "chunks = []\n"
            "current = []\n"
            "for line in text.splitlines(keepends=True):\n"
            "    if line.startswith('From '):\n"
            "        if current:\n"
            "            chunks.append(''.join(current))\n"
            "            current = []\n"
            "        continue\n"
            "    current.append(line)\n"
            "if current:\n"
            "    chunks.append(''.join(current))\n"
            "out_dir = Path(os.environ['SECTION_DIR'])\n"
            "count = 0\n"
            "for chunk in chunks:\n"
            "    lines = chunk.splitlines(keepends=True)\n"
            "    start = next((i for i, line in enumerate(lines) if line.startswith('diff --git ') or line.startswith('--- ')), None)\n"
            "    if start is None:\n"
            "        continue\n"
            "    section = ''.join(lines[start:])\n"
            "    if not section.strip():\n"
            "        continue\n"
            "    count += 1\n"
            "    (out_dir / f'section_{count}.diff').write_text(section, encoding='latin-1')\n"
            "print(count)\n"
            "PY\n); "
            "apply_one() { "
            "  local patch_file=\"$1\"; "
            "  if git apply \"${GIT_APPLY_EXTRA[@]}\" --allow-empty \"$patch_file\" 2>/tmp/lhb_apply.err; then return 0; fi; "
            "  if grep -qi 'unknown option' /tmp/lhb_apply.err || grep -qi 'usage:' /tmp/lhb_apply.err; then "
            "    git reset --hard -q HEAD >/dev/null 2>&1 || true; "
            "    git clean -fdq >/dev/null 2>&1 || true; "
            "    git apply \"${GIT_APPLY_EXTRA[@]}\" \"$patch_file\" 2>/tmp/lhb_apply.err && return 0; "
            "  fi; "
            "  git reset --hard -q HEAD >/dev/null 2>&1 || true; "
            "  git clean -fdq >/dev/null 2>&1 || true; "
            "  git apply \"${GIT_APPLY_EXTRA[@]}\" --3way --allow-empty \"$patch_file\" 2>/tmp/lhb_apply.err && return 0; "
            "  git reset --hard -q HEAD >/dev/null 2>&1 || true; "
            "  git clean -fdq >/dev/null 2>&1 || true; "
            f"  ( cd {patch_cd_q} && patch -p1 --forward --no-backup-if-mismatch -i \"$patch_file\" ) 2>/tmp/lhb_apply.err && return 0; "
            "  git reset --hard -q HEAD >/dev/null 2>&1 || true; "
            "  git clean -fdq >/dev/null 2>&1 || true; "
            f"  ( cd {patch_cd_q} && patch -p1 --fuzz=3 --forward --no-backup-if-mismatch -i \"$patch_file\" ) 2>/tmp/lhb_apply.err && return 0; "
            "  git reset --hard -q HEAD >/dev/null 2>&1 || true; "
            "  git clean -fdq >/dev/null 2>&1 || true; "
            "  return 1; "
            "}; "
            "if [ \"$section_count\" -gt 1 ]; then "
            "  for section_patch in \"$section_dir\"/section_*.diff; do "
            "    apply_one \"$section_patch\" || { cat /tmp/lhb_apply.err >&2; rm -f /tmp/lhb_apply.err; rm -rf \"$section_dir\"; exit 1; }; "
            "  done; "
            "else "
            f"  apply_one {patch_q} || {{ cat /tmp/lhb_apply.err >&2; rm -f /tmp/lhb_apply.err; rm -rf \"$section_dir\"; exit 1; }}; "
            "fi; "
            "rm -f /tmp/lhb_apply.err; "
            "rm -rf \"$section_dir\"; "
            f"git -C {repo_q} add -A; "
            f"git -C {repo_q} commit -m {commit_q} --allow-empty"
        )
    else:
        script = (
            "set -e; "
            f"cd {patch_cd_q}; "
            f"patch -p1 --no-backup-if-mismatch < {patch_q}"
        )
    rc, stdout, stderr = docker_exec(container, ["bash", "-c", script], timeout=timeout)
    return {
        "ok": rc == 0,
        "returncode": rc,
        "stdout": _tail(stdout),
        "stderr": _tail(stderr),
        "patch_in_container": patch_in_container,
        "used_git": use_git,
    }


def stage_pr_tests(container: str, repo_root: str, pr_num: str, test_files: list[str], timeout: int = 120, excluded_repo_paths: set[str] | None = None) -> dict:
    excluded_repo_paths = sorted(excluded_repo_paths or set())
    lines = [
        "import importlib.util, json, os, shutil",
        f"repo_root = {repo_root!r}",
        f"pr_num = {pr_num!r}",
        f"test_files = {test_files!r}",
        f"excluded_repo_paths = {excluded_repo_paths!r}",
        "tests_root = '/tests'",
        "source_roots = [tests_root] if pr_num == 'gold' else [os.path.join(tests_root, pr_num), os.path.join(tests_root, f'pr_{pr_num}'), os.path.join(tests_root, 'tests')]",
        "if not any(os.path.isdir(p) for p in source_roots):",
        "    raise SystemExit('missing_pr_test_dir')",
        "def clear_cached_bytecode(path):",
        "    if not path.endswith('.py'):",
        "        return",
        "    try:",
        "        cached = importlib.util.cache_from_source(path)",
        "    except (NotImplementedError, ValueError):",
        "        return",
        "    if os.path.exists(cached):",
        "        os.remove(cached)",
        "backup_root = os.path.join('/tmp/lhb_test_backup', pr_num)",
        "os.makedirs(backup_root, exist_ok=True)",
        "manifest = []",
        "for rel in test_files:",
        "    if rel in excluded_repo_paths:",
        "        continue",
        "    candidate_rels = [rel]",
        "    if pr_num != 'gold' and rel.startswith('tests/'):",
        "        candidate_rels.append(rel[len('tests/'):])",
        "    src = next((os.path.join(root, candidate_rel) for candidate_rel in candidate_rels for root in source_roots if os.path.isfile(os.path.join(root, candidate_rel))), None)",
        "    dst = os.path.join(repo_root, rel)",
        "    bak = os.path.join(backup_root, rel)",
        "    if src is None:",
        "        raise SystemExit(f'missing_test_file:{rel}')",
        "    os.makedirs(os.path.dirname(dst), exist_ok=True)",
        "    if os.path.exists(dst):",
        "        os.makedirs(os.path.dirname(bak), exist_ok=True)",
        "        shutil.copy2(dst, bak)",
        "        existed = True",
        "    else:",
        "        existed = False",
        "    shutil.copyfile(src, dst)",
        "    clear_cached_bytecode(dst)",
        "    manifest.append({'rel': rel, 'existed': existed})",
        "with open(os.path.join(backup_root, 'manifest.json'), 'w') as fh:",
        "    json.dump(manifest, fh)",
    ]
    rc, stdout, stderr = docker_exec(container, ["python3", "-c", "\n".join(lines)], timeout=timeout)
    return {"ok": rc == 0, "returncode": rc, "stdout": _tail(stdout), "stderr": _tail(stderr)}


def stage_pr_apply_support_files(container: str, repo_root: str, pr_num: str, support_files: list[str], timeout: int = 120) -> dict:
    lines = [
        "import importlib.util, os, shutil",
        f"repo_root = {repo_root!r}",
        f"pr_num = {pr_num!r}",
        f"support_files = {support_files!r}",
        "tests_root = '/tests'",
        "source_roots = [tests_root] if pr_num == 'gold' else [os.path.join(tests_root, pr_num), os.path.join(tests_root, f'pr_{pr_num}'), os.path.join(tests_root, 'tests')]",
        "if not any(os.path.isdir(p) for p in source_roots):",
        "    raise SystemExit('missing_pr_test_dir')",
        "def clear_cached_bytecode(path):",
        "    if not path.endswith('.py'):",
        "        return",
        "    try:",
        "        cached = importlib.util.cache_from_source(path)",
        "    except (NotImplementedError, ValueError):",
        "        return",
        "    if os.path.exists(cached):",
        "        os.remove(cached)",
        "for rel in support_files:",
        "    candidate_rels = [rel]",
        "    if pr_num != 'gold' and rel.startswith('tests/'):",
        "        candidate_rels.append(rel[len('tests/'):])",
        "    src = next((os.path.join(root, candidate_rel) for candidate_rel in candidate_rels for root in source_roots if os.path.isfile(os.path.join(root, candidate_rel))), None)",
        "    if src is None:",
        "        raise SystemExit(f'missing_support_file:{rel}')",
        "    dst = os.path.join(repo_root, rel)",
        "    os.makedirs(os.path.dirname(dst), exist_ok=True)",
        "    shutil.copyfile(src, dst)",
        "    clear_cached_bytecode(dst)",
    ]
    rc, stdout, stderr = docker_exec(container, ["python3", "-c", "\n".join(lines)], timeout=timeout)
    if rc != 0:
        return {"ok": False, "returncode": rc, "stdout": _tail(stdout), "stderr": _tail(stderr)}
    add_rc, add_stdout, add_stderr = docker_exec(
        container,
        ["git", "-C", repo_root, "add", "--", *support_files],
        timeout=timeout,
    )
    return {
        "ok": add_rc == 0,
        "returncode": add_rc,
        "stdout": _tail(stdout + add_stdout),
        "stderr": _tail(stderr + add_stderr),
    }


def restore_pr_tests(container: str, repo_root: str, pr_num: str, timeout: int = 120) -> tuple[int, str, str]:
    lines = [
        "import json, os, shutil",
        f"repo_root = {repo_root!r}",
        f"pr_num = {pr_num!r}",
        "backup_root = os.path.join('/tmp/lhb_test_backup', pr_num)",
        "manifest_path = os.path.join(backup_root, 'manifest.json')",
        "if not os.path.isfile(manifest_path):",
        "    raise SystemExit(0)",
        "with open(manifest_path) as fh:",
        "    manifest = json.load(fh)",
        "for item in manifest:",
        "    rel = item['rel']",
        "    dst = os.path.join(repo_root, rel)",
        "    bak = os.path.join(backup_root, rel)",
        "    if item.get('existed') and os.path.exists(bak):",
        "        os.makedirs(os.path.dirname(dst), exist_ok=True)",
        "        shutil.copy2(bak, dst)",
        "    elif os.path.exists(dst):",
        "        os.remove(dst)",
        "shutil.rmtree(backup_root, ignore_errors=True)",
    ]
    return docker_exec(container, ["python3", "-c", "\n".join(lines)], timeout=timeout)


def mask_unselected_java_tests(container: str, repo_root: str, pr_num: str, selected_test_files: list[str], timeout: int = 120) -> dict:
    selected_test_files = sorted({path for path in selected_test_files if "/src/test/" in path})
    if not selected_test_files:
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

    lines = [
        "import json, os, shutil",
        f"repo_root = {repo_root!r}",
        f"pr_num = {pr_num!r}",
        f"selected_test_files = {selected_test_files!r}",
        "backup_root = os.path.join('/tmp/lhb_test_backup', pr_num)",
        "os.makedirs(backup_root, exist_ok=True)",
        "manifest_path = os.path.join(backup_root, 'masked_java_tests.json')",
        "selected = set(selected_test_files)",
        "test_roots = sorted({path.split('/src/test/', 1)[0] + '/src/test' for path in selected_test_files if '/src/test/' in path})",
        "masked = []",
        "for test_root in test_roots:",
        "    abs_root = os.path.join(repo_root, test_root)",
        "    if not os.path.isdir(abs_root):",
        "        continue",
        "    for root, _, files in os.walk(abs_root):",
        "        for name in files:",
        "            abs_path = os.path.join(root, name)",
        "            rel_path = os.path.relpath(abs_path, repo_root)",
        "            if rel_path in selected:",
        "                continue",
        "            backup_path = os.path.join(backup_root, rel_path)",
        "            os.makedirs(os.path.dirname(backup_path), exist_ok=True)",
        "            shutil.move(abs_path, backup_path)",
        "            masked.append(rel_path)",
        "with open(manifest_path, 'w') as fh:",
        "    json.dump(masked, fh)",
    ]
    rc, stdout, stderr = docker_exec(container, ["python3", "-c", "\n".join(lines)], timeout=timeout)
    return {"ok": rc == 0, "returncode": rc, "stdout": _tail(stdout), "stderr": _tail(stderr)}


def restore_masked_java_tests(container: str, repo_root: str, pr_num: str, timeout: int = 120) -> tuple[int, str, str]:
    lines = [
        "import json, os, shutil",
        f"repo_root = {repo_root!r}",
        f"pr_num = {pr_num!r}",
        "backup_root = os.path.join('/tmp/lhb_test_backup', pr_num)",
        "manifest_path = os.path.join(backup_root, 'masked_java_tests.json')",
        "if not os.path.isfile(manifest_path):",
        "    raise SystemExit(0)",
        "with open(manifest_path) as fh:",
        "    masked = json.load(fh)",
        "for rel in masked:",
        "    src = os.path.join(backup_root, rel)",
        "    dst = os.path.join(repo_root, rel)",
        "    if not os.path.exists(src):",
        "        continue",
        "    os.makedirs(os.path.dirname(dst), exist_ok=True)",
        "    shutil.move(src, dst)",
        "os.remove(manifest_path)",
    ]
    return docker_exec(container, ["python3", "-c", "\n".join(lines)], timeout=timeout)


def run_pr_tests(container: str, lang: str, repo_id: str, repo_root: str, task_dir: Path, pr_num: str, test_files: list[str], timeout: int, test_labels: list[str] | None = None) -> dict:
    repo_root_q = shlex.quote(repo_root)
    if pr_num == "gold":
        native_cmd = ["bash", "-c", f"cd {repo_root_q} && TEST_DIR=/tests bash run-tests.sh"]
        rc, stdout, stderr = docker_exec(container, native_cmd, timeout=timeout)
    else:
        explicit_selected_tests = _get_explicit_selected_tests(task_dir, pr_num)
        if repo_id == "django" and test_labels:
            joined = " ".join(shlex.quote(label) for label in test_labels)
            native_cmd = [
                "bash",
                "-c",
                f"set -o pipefail; cd {repo_root_q}/tests && RUNNING_DJANGOS_TEST_SUITE=true PYTHONPATH={repo_root_q} python3 runtests.py --settings=test_sqlite --parallel=1 {joined} 2>&1 | tail -50",
            ]
        elif explicit_selected_tests is not None:
            native_cmd = build_native_test_cmd(repo_id=repo_id, test_files=explicit_selected_tests, repo_root=repo_root)
        else:
            native_cmd = build_native_test_cmd(repo_id=repo_id, test_files=test_files, repo_root=repo_root)
        if repo_id in {"framework", "cs122"} and (task_dir / "tests" / "test_unit_runner.py").is_file():
            nodeid = shlex.quote(f"test_unit_runner.py::test_unit[{pr_num}]")
            # cs122 installs pytest only in /opt/test-venv; framework images use python3-pytest on PATH.
            native_cmd = [
                "bash",
                "-c",
                f"set -o pipefail; cd /tests && UNIT_ID={shlex.quote(pr_num)} "
                f'PY="python3"; [ -x /opt/test-venv/bin/python ] && PY=/opt/test-venv/bin/python; '
                f"$PY -m pytest -v {nodeid} 2>&1 | tail -50",
            ]
            rc, stdout, stderr = docker_exec(container, native_cmd, timeout=timeout)
        elif repo_id == "navidrome" and (task_dir / "tests" / "test_unit_runner.py").is_file():
            nodeid = shlex.quote(f"test_unit_runner.py::test_unit[{pr_num}]")
            prep_cmd = [
                "bash",
                "-c",
                f"if [ -f {repo_root_q}/go.mod ]; then "
                f"(cd {repo_root_q} && go mod download all >/dev/null 2>&1 || go mod download >/dev/null 2>&1 || true); "
                f"fi",
            ]
            docker_exec(container, prep_cmd, timeout=timeout)
            native_cmd = [
                "bash",
                "-c",
                f"set -o pipefail; cd /tests && export TESTS_DIR=/tests && export UNIT_ID={shlex.quote(pr_num)} && "
                f'PY="python3"; [ -x /opt/test-venv/bin/python ] && PY=/opt/test-venv/bin/python; '
                f"$PY -m pytest -v {nodeid} 2>&1 | tail -80",
            ]
            rc, stdout, stderr = docker_exec(container, native_cmd, timeout=timeout)
        elif repo_id in {"fdsql", "hgdstru", "uttaos"} and (task_dir / "tests" / "test_unit_runner.py").is_file():
            nodeid = shlex.quote(f"test_unit_runner.py::test_unit[{pr_num}]")
            unit_export = ""
            tail_lines = "80"
            if repo_id == "uttaos":
                unit_export = f"export UNIT_ID={shlex.quote(pr_num)}; "
                tail_lines = "200"
            native_cmd = [
                "bash",
                "-c",
                f"set -o pipefail; cd /tests && {unit_export}"
                f'PY="python3"; [ -x /opt/test-venv/bin/python ] && PY=/opt/test-venv/bin/python; '
                f"$PY -m pytest -v {nodeid} 2>&1 | tail -{tail_lines}",
            ]
            rc, stdout, stderr = docker_exec(container, native_cmd, timeout=timeout)
        elif _uses_pr_test_task_runner_path(task_dir, lang):
            pr_test_dir = "/tests" if pr_num == "gold" else f"/tests/{pr_num}"
            pr_test_dir_q = shlex.quote(pr_test_dir)
            native_cmd = [
                "bash",
                "-c",
                f"export PR_TEST_DIR={pr_test_dir_q} && export UNIT_TEST_DIR={pr_test_dir_q} && "
                f"source /opt/test-venv/bin/activate && "
                f"python -m pytest -x --tb=short -q /tests/test_task_runner.py",
            ]
            rc, stdout, stderr = docker_exec(container, native_cmd, timeout=timeout)
        elif repo_id != "django" and (task_dir / "tests" / "test_unit_runner.py").is_file():
            nodeid = shlex.quote(f"test_unit_runner.py::test_unit[{pr_num}]")
            native_cmd = [
                "bash",
                "-c",
                f"set -o pipefail; cd /tests && "
                f'PY="python3"; [ -x /opt/test-venv/bin/python ] && PY=/opt/test-venv/bin/python; '
                f"$PY -m pytest -v {nodeid} 2>&1 | tail -80",
            ]
            rc, stdout, stderr = docker_exec(container, native_cmd, timeout=timeout)
        else:
            rc, stdout, stderr = docker_exec(container, native_cmd, timeout=timeout)
    tail_limit = 12000 if repo_id == "uttaos" else 800
    return {
        "returncode": rc,
        "passed": rc == 0,
        "stdout": _tail(stdout, tail_limit),
        "stderr": _tail(stderr, tail_limit),
        "command": native_cmd,
    }


def copy_task_runtime_assets(container: str, task_dir: Path, repo_root: str, timeout: int = 180) -> dict:
    repo_q = shlex.quote(repo_root)
    steps: list[dict] = []

    def record_step(name: str, rc: int, stdout: str, stderr: str):
        steps.append({"step": name, "returncode": rc, "stdout": _tail(stdout), "stderr": _tail(stderr)})
        return rc == 0

    rc, stdout, stderr = docker_exec(container, ["bash", "-c", f"mkdir -p {repo_q}"], timeout=30)
    if not record_step("mkdir_repo_root", rc, stdout, stderr):
        return {"ok": False, "steps": steps}

    run_tests = task_dir / "run-tests.sh"
    if not run_tests.is_file():
        steps.append({"step": "missing_run_tests", "returncode": 1, "stdout": "", "stderr": "run-tests.sh missing"})
        return {"ok": False, "steps": steps}
    rc, stdout, stderr = copy_host_path_to_container(run_tests, container, f"{repo_root}/run-tests.sh", timeout=timeout)
    if not record_step("copy_run_tests", rc, stdout, stderr):
        return {"ok": False, "steps": steps}

    solution = task_dir / "solution.sh"
    if solution.is_file():
        rc, stdout, stderr = copy_host_path_to_container(solution, container, f"{repo_root}/solution.sh", timeout=timeout)
        if not record_step("copy_solution", rc, stdout, stderr):
            return {"ok": False, "steps": steps}

    for metadata_name in ("patch_order.json", "unit_dag.json", "slug_diff_map.json"):
        metadata_path = task_dir / metadata_name
        if metadata_path.is_file():
            rc, stdout, stderr = copy_host_path_to_container(metadata_path, container, f"{repo_root}/{metadata_name}", timeout=timeout)
            if not record_step(f"copy_{metadata_name}", rc, stdout, stderr):
                return {"ok": False, "steps": steps}

    gold_dir = task_dir / "gold_patches"
    if gold_dir.is_dir():
        rc, stdout, stderr = docker_exec(container, ["bash", "-c", f"mkdir -p {repo_q}/gold_patches"], timeout=30)
        if not record_step("mkdir_gold_patches", rc, stdout, stderr):
            return {"ok": False, "steps": steps}
        rc, stdout, stderr = copy_tree_to_container(gold_dir, container, f"{repo_root}/gold_patches", timeout=timeout)
        if not record_step("copy_gold_patches", rc, stdout, stderr):
            return {"ok": False, "steps": steps}

    monolithic = task_dir / "gold-patch.diff"
    monolithic_underscore = task_dir / "gold_patch.diff"
    if monolithic.is_file():
        rc, stdout, stderr = copy_host_path_to_container(monolithic, container, f"{repo_root}/gold-patch.diff", timeout=timeout)
        if not record_step("copy_gold_patch", rc, stdout, stderr):
            return {"ok": False, "steps": steps}
    elif monolithic_underscore.is_file():
        rc, stdout, stderr = copy_host_path_to_container(
            monolithic_underscore, container, f"{repo_root}/gold_patch.diff", timeout=timeout
        )
        if not record_step("copy_gold_patch_underscore", rc, stdout, stderr):
            return {"ok": False, "steps": steps}

    rc, stdout, stderr = docker_exec(container, ["bash", "-c", f"chmod +x {repo_q}/run-tests.sh {repo_q}/solution.sh 2>/dev/null || true"], timeout=30)
    record_step("chmod_scripts", rc, stdout, stderr)
    return {"ok": True, "steps": steps}


def run_task_tests(container: str, repo_root: str, timeout: int) -> dict:
    repo_q = shlex.quote(repo_root)
    native_cmd = ["bash", "-c", f"cd {repo_q} && TEST_DIR=/tests bash run-tests.sh"]
    rc, stdout, stderr = docker_exec(container, native_cmd, timeout=timeout)
    return {
        "returncode": rc,
        "passed": rc == 0,
        "stdout": _tail(stdout),
        "stderr": _tail(stderr),
        "command": native_cmd,
    }


def run_solution_script(container: str, repo_root: str, timeout: int) -> dict:
    repo_q = shlex.quote(repo_root)
    native_cmd = ["bash", "-c", f"cd {repo_q} && bash solution.sh"]
    rc, stdout, stderr = docker_exec(container, native_cmd, timeout=timeout)
    return {
        "returncode": rc,
        "ok": rc == 0,
        "stdout": _tail(stdout),
        "stderr": _tail(stderr),
        "command": native_cmd,
    }


def _get_java_test_modules(test_files: list[str]) -> list[str]:
    modules: set[str] = set()
    for path in test_files:
        if not path.endswith(".java"):
            continue
        if "/src/test/java/" in path:
            module = path.split("/src/test/java/", 1)[0]
        elif "/test/" in path:
            module = path.split("/test/", 1)[0]
        else:
            continue
        modules.add(module or ".")
    return sorted(modules)


def clean_java_build_outputs(container: str, repo_root: str, test_files: list[str], timeout: int = 120) -> dict:
    modules = _get_java_test_modules(test_files)
    if not modules:
        return {
            "returncode": 0,
            "ok": True,
            "stdout": "",
            "stderr": "",
            "command": ["bash", "-c", "true"],
            "modules": [],
        }

    repo_q = shlex.quote(repo_root)
    module_targets = " ".join(shlex.quote(f"{module}/target" if module != "." else "target") for module in modules)
    native_cmd = [
        "bash",
        "-c",
        f"cd {repo_q} && rm -rf {module_targets}",
    ]
    rc, stdout, stderr = docker_exec(container, native_cmd, timeout=timeout)
    return {
        "returncode": rc,
        "ok": rc == 0,
        "stdout": _tail(stdout),
        "stderr": _tail(stderr),
        "command": native_cmd,
        "modules": modules,
    }


def _skip_git_clean_fdX_for_task(task_dir: Path) -> bool:
    """If True, reset uses only `git clean -fdq` (no `-fdX`).

    task_cmu15-462: Dockerfile extracts `nest-libs/` after COPY base; base/.gitignore lists it.
    `git clean -fdX` removes ignored paths and deletes nest-libs, breaking Maekfile.js / pytest.
    task_node: `make node` produces ignored build outputs (`out/`, `node`, `config.status`) reused across PRs.
    """
    rid = infer_repo_id(task_dir, fallback_repo_id=task_dir.name.removeprefix("task_"))
    return rid in {"cmu15-462", "echarts", "NodeBB", "node"}


def reset_repo_workspace(
    container: str,
    repo_root: str,
    timeout: int = 120,
    *,
    skip_git_clean_fdX: bool = False,
) -> dict:
    """Reset to the repo root commit so multi-PR runs in one container return to hollow base."""
    repo_q = shlex.quote(repo_root)
    if skip_git_clean_fdX:
        clean_tail = "git clean -fdq >/dev/null 2>&1"
    else:
        clean_tail = "git clean -fdq >/dev/null 2>&1 && git clean -fdXq >/dev/null 2>&1"
    native_cmd = [
        "bash",
        "-c",
        f"cd {repo_q} && "
        f"root=$(git rev-list --max-parents=0 HEAD 2>/dev/null | tail -n 1); "
        f"if [ -n \"$root\" ]; then git reset --hard -q \"$root\" >/dev/null 2>&1; "
        f"else git reset --hard -q HEAD >/dev/null 2>&1; fi && "
        f"{clean_tail}",
    ]
    rc, stdout, stderr = docker_exec(container, native_cmd, timeout=timeout)
    return {
        "returncode": rc,
        "ok": rc == 0,
        "stdout": _tail(stdout),
        "stderr": _tail(stderr),
        "command": native_cmd,
    }


def validate_task_strict(container: str, repo_root: str, task_dir: Path, test_timeout: int) -> dict:
    result = {
        "pr": "task",
        "tested": True,
        "status": "pass",
        "failure_stage": None,
        "failure_category": None,
        "failure_detail": None,
        "before": None,
        "after": None,
        "patch_apply": None,
        "runtime_assets": None,
    }

    assets = copy_task_runtime_assets(container, task_dir, repo_root)
    result["runtime_assets"] = assets
    if not assets["ok"]:
        result["status"] = "fail_stage_runtime_assets"
        result["failure_stage"] = "stage_runtime"
        return result

    before = run_task_tests(container, repo_root, test_timeout)
    result["before"] = before
    if before["passed"]:
        result["status"] = "fail_before_not_failing"
        result["failure_stage"] = "before_tests"
        return result

    cleanup = reset_repo_workspace(
        container,
        repo_root,
        skip_git_clean_fdX=_skip_git_clean_fdX_for_task(task_dir),
    )
    result["workspace_reset"] = cleanup
    if not cleanup["ok"]:
        result["status"] = "fail_reset_workspace"
        result["failure_stage"] = "reset_workspace"
        return result

    assets = copy_task_runtime_assets(container, task_dir, repo_root)
    result["runtime_assets_after_reset"] = assets
    if not assets["ok"]:
        result["status"] = "fail_stage_runtime_assets_after_reset"
        result["failure_stage"] = "stage_runtime"
        return result

    apply_result = run_solution_script(container, repo_root, test_timeout)
    result["patch_apply"] = apply_result
    if not apply_result["ok"]:
        result["status"] = "fail_patch_apply"
        result["failure_stage"] = "apply_patch"
        return result

    after = run_task_tests(container, repo_root, test_timeout)
    result["after"] = after
    if not after["passed"]:
        result["status"] = "fail_after_not_passing"
        result["failure_stage"] = "after_tests"
        attach_failure_classification(result, after)
        return result

    return result


def _apply_patch_chain_units(
    container: str,
    repo_root: str,
    repo_id: str,
    task_dir: Path,
    chain_units: list[str],
    *,
    test_files: list[str] | None = None,
    apply_timeout: int = 120,
    patch_cwd: str | None = None,
) -> dict:
    combined_stdout: list[str] = []
    combined_stderr: list[str] = []
    chain_touched_files: set[str] = set()
    last_apply: dict | None = None
    for chain_unit in chain_units:
        patch_host_path = resolve_patch_host_path(task_dir, chain_unit)
        if patch_host_path is None:
            return {
                "ok": False,
                "returncode": 1,
                "stdout": _tail("".join(combined_stdout)),
                "stderr": _tail("".join(combined_stderr + [f"missing patch for unit {chain_unit!r}"])),
                "after_patch_chain": list(chain_units),
            }

        patch_container_path = f"/tmp/lhb_patches/current_{chain_unit}.diff"
        rc, stdout, stderr = copy_host_path_to_container(
            patch_host_path,
            container,
            patch_container_path,
        )
        combined_stdout.append(stdout or "")
        combined_stderr.append(stderr or "")
        if rc != 0:
            return {
                "ok": False,
                "returncode": rc,
                "stdout": _tail("".join(combined_stdout)),
                "stderr": _tail("".join(combined_stderr)),
                "after_patch_chain": list(chain_units),
            }

        chain_touched = get_patch_touched_files(patch_host_path)
        chain_touched_files.update(chain_touched)
        apply_support_files: list[str] = []
        if repo_id == "django" and test_files:
            apply_support_files = sorted(
                rel
                for rel in test_files
                if not rel.startswith("tests/") and rel in chain_touched
            )
        if apply_support_files:
            stage_apply_support = stage_pr_apply_support_files(
                container,
                repo_root,
                chain_unit,
                apply_support_files,
            )
            if not stage_apply_support["ok"]:
                return {
                    **stage_apply_support,
                    "after_patch_chain": list(chain_units),
                }
            combined_stdout.append(stage_apply_support.get("stdout") or "")
            combined_stderr.append(stage_apply_support.get("stderr") or "")

        apply_result = apply_patch_and_commit(
            container,
            repo_root,
            patch_container_path,
            chain_unit,
            apply_timeout,
            patch_cwd=patch_cwd,
        )
        last_apply = apply_result
        combined_stdout.append(apply_result.get("stdout") or "")
        combined_stderr.append(apply_result.get("stderr") or "")
        if not apply_result["ok"]:
            return {
                **apply_result,
                "after_patch_chain": list(chain_units),
                "stdout": _tail("".join(combined_stdout)),
                "stderr": _tail("".join(combined_stderr)),
            }

    assert last_apply is not None
    refresh_result = _post_patch_runtime_refresh(
        container,
        repo_id,
        repo_root,
        chain_touched_files,
        timeout=max(apply_timeout, 300),
    )
    combined_stdout.append(refresh_result.get("stdout") or "")
    combined_stderr.append(refresh_result.get("stderr") or "")
    if not refresh_result["ok"]:
        return {
            **refresh_result,
            "after_patch_chain": list(chain_units),
            "stdout": _tail("".join(combined_stdout)),
            "stderr": _tail("".join(combined_stderr)),
        }
    return {
        **last_apply,
        "after_patch_chain": list(chain_units),
        "stdout": _tail("".join(combined_stdout)),
        "stderr": _tail("".join(combined_stderr)),
    }


def _apply_current_pr_patch(
    container: str,
    repo_root: str,
    repo_id: str,
    task_dir: Path,
    pr_num: str,
    *,
    test_files: list[str] | None = None,
    apply_timeout: int = 120,
    patch_cwd: str | None = None,
) -> dict:
    return _apply_patch_chain_units(
        container=container,
        repo_root=repo_root,
        repo_id=repo_id,
        task_dir=task_dir,
        chain_units=[pr_num],
        test_files=test_files,
        apply_timeout=apply_timeout,
        patch_cwd=patch_cwd,
    )


def _get_repo_head_commit(container: str, repo_root: str, timeout: int = 30) -> str | None:
    rc, stdout, _stderr = docker_exec(
        container,
        ["bash", "-c", f"cd {shlex.quote(repo_root)} && git rev-parse HEAD"],
        timeout=timeout,
    )
    if rc != 0:
        return None
    head = (stdout or "").strip().splitlines()
    return head[-1].strip() if head else None


def _reset_repo_to_commit(
    container: str,
    repo_root: str,
    commit: str,
    timeout: int = 120,
    *,
    skip_git_clean_fdX: bool = False,
) -> dict:
    repo_q = shlex.quote(repo_root)
    commit_q = shlex.quote(commit)
    if skip_git_clean_fdX:
        clean_tail = "git clean -fdq >/dev/null 2>&1"
    else:
        clean_tail = "git clean -fdq >/dev/null 2>&1 && git clean -fdXq >/dev/null 2>&1"
    native_cmd = [
        "bash",
        "-c",
        f"cd {repo_q} && git reset --hard -q {commit_q} >/dev/null 2>&1 && {clean_tail}",
    ]
    rc, stdout, stderr = docker_exec(container, native_cmd, timeout=timeout)
    return {
        "returncode": rc,
        "ok": rc == 0,
        "stdout": _tail(stdout),
        "stderr": _tail(stderr),
        "command": native_cmd,
    }


def validate_tested_pr_strict_cumulative(
    container: str,
    repo_root: str,
    lang: str,
    repo_id: str,
    task_dir: Path,
    pr_num: str,
    test_timeout: int,
) -> dict:
    test_files = get_pr_test_files(task_dir, pr_num)
    test_labels = get_django_test_labels(task_dir, pr_num, test_files) if repo_id == "django" else None
    patch_host_path = resolve_patch_host_path(task_dir, pr_num)
    patch_touched_files = get_patch_touched_files(patch_host_path)
    excluded_before_test_paths = patch_touched_files if repo_id != "django" else set()
    result = {
        "pr": pr_num,
        "tested": True,
        "status": "pass",
        "failure_stage": None,
        "failure_category": None,
        "failure_detail": None,
        "test_files": test_files,
        "test_labels": test_labels,
        "patch_host_path": str(patch_host_path) if patch_host_path else None,
        "before": None,
        "after": None,
        "patch_apply": None,
        "runtime_assets": None,
    }

    if patch_host_path is None:
        result["status"] = "fail_patch_missing"
        result["failure_stage"] = "apply_patch"
        return result

    explicit_selected_tests = _get_explicit_selected_tests(task_dir, pr_num)
    apply_patch_cwd = infer_patch_apply_cwd(task_dir, repo_root)
    apply_timeout = max(120, test_timeout)
    after_patch_chain = _strict_after_patch_chain(task_dir, pr_num)
    prefix_commit = _get_repo_head_commit(container, repo_root)
    if not prefix_commit:
        result["status"] = "fail_reset_workspace"
        result["failure_stage"] = "reset_workspace"
        result["before_restore"] = {
            "ok": False,
            "returncode": 1,
            "stdout": "",
            "stderr": "unable to capture prefix HEAD before before-tests",
        }
        return result

    def apply_current_patch() -> dict:
        patch_apply = _apply_current_pr_patch(
            container=container,
            repo_root=repo_root,
            repo_id=repo_id,
            task_dir=task_dir,
            pr_num=pr_num,
            test_files=test_files,
            apply_timeout=apply_timeout,
            patch_cwd=apply_patch_cwd,
        )
        result["patch_apply"] = patch_apply
        if not patch_apply["ok"]:
            result["status"] = "fail_patch_apply"
            result["failure_stage"] = "apply_patch"
        return patch_apply

    before = None
    if not test_files or (repo_id == "django" and not test_labels):
        result["tested"] = False
        result["status"] = "skip_no_tests"
        apply_current_patch()
        return result
    if explicit_selected_tests == []:
        result["tested"] = False
        result["status"] = "skip_no_tests"
        apply_current_patch()
        return result

    stage_result = stage_pr_tests(
        container,
        repo_root,
        pr_num,
        test_files,
        excluded_repo_paths=excluded_before_test_paths,
    )
    if not stage_result["ok"]:
        result["status"] = "fail_stage_tests"
        result["failure_stage"] = "before_tests"
        result["before"] = stage_result
        return result

    before_cleanup = None
    before_mask = None
    try:
        if lang == "java" and explicit_selected_tests is not None:
            before_mask = mask_unselected_java_tests(container, repo_root, pr_num, explicit_selected_tests)
            result["before_mask"] = before_mask
            if not before_mask["ok"]:
                result["status"] = "fail_before_mask"
                result["failure_stage"] = "before_tests"
                result["before"] = before_mask
                return result
        if lang == "java":
            cleanup_test_files = explicit_selected_tests if explicit_selected_tests is not None else test_files
            before_cleanup = clean_java_build_outputs(container, repo_root, cleanup_test_files)
            result["before_cleanup"] = before_cleanup
            if not before_cleanup["ok"]:
                result["status"] = "fail_before_cleanup"
                result["failure_stage"] = "before_tests"
                result["before"] = before_cleanup
                return result
        before = run_pr_tests(container, lang, repo_id, repo_root, task_dir, pr_num, test_files, test_timeout, test_labels=test_labels)
        result["before"] = before
    finally:
        if lang == "java" and explicit_selected_tests is not None:
            restore_masked_java_tests(container, repo_root, pr_num)
        restore_pr_tests(container, repo_root, pr_num)
        restore_prefix = _reset_repo_to_commit(
            container,
            repo_root,
            commit=prefix_commit,
            timeout=max(120, test_timeout),
            skip_git_clean_fdX=_skip_git_clean_fdX_for_task(task_dir),
        )
        result["before_restore"] = restore_prefix
        if not restore_prefix["ok"]:
            result["status"] = "fail_reset_workspace"
            result["failure_stage"] = "reset_workspace"

    if result["status"] == "fail_reset_workspace":
        return result

    before_passed = before["passed"]
    before_only_skips = repo_id == "django" and _django_run_is_only_skips(before)
    before_backend_gated = repo_id == "django" and _django_run_is_backend_gated(before)

    if before_backend_gated:
        result["tested"] = False
        result["status"] = "skip_no_tests"
        apply_current_patch()
        return result
    if before_passed and before_only_skips:
        result["tested"] = False
        result["status"] = "skip_no_tests"
        apply_current_patch()
        return result

    patch_apply = apply_current_patch()
    patch_apply["after_patch_chain"] = list(after_patch_chain)
    if not patch_apply["ok"]:
        return result

    extra_units: list[str] = []
    if pr_num in after_patch_chain:
        extra_units = after_patch_chain[after_patch_chain.index(pr_num) + 1 :]

    restore_commit: str | None = None
    if extra_units:
        restore_commit = _get_repo_head_commit(container, repo_root)
        if not restore_commit:
            result["status"] = "fail_reset_workspace"
            result["failure_stage"] = "reset_workspace"
            result["after_restore"] = {
                "ok": False,
                "returncode": 1,
                "stdout": "",
                "stderr": "unable to capture current HEAD before after-state extension",
            }
            return result
        extra_apply = _apply_patch_chain_units(
            container=container,
            repo_root=repo_root,
            repo_id=repo_id,
            task_dir=task_dir,
            chain_units=extra_units,
            test_files=test_files,
            apply_timeout=apply_timeout,
            patch_cwd=apply_patch_cwd,
        )
        patch_apply = {
            **patch_apply,
            "after_patch_chain": list(after_patch_chain),
            "stdout": _tail((patch_apply.get("stdout") or "") + (extra_apply.get("stdout") or "")),
            "stderr": _tail((patch_apply.get("stderr") or "") + (extra_apply.get("stderr") or "")),
        }
        result["patch_apply"] = patch_apply
        if not extra_apply["ok"]:
            restore_result = _reset_repo_to_commit(
                container,
                repo_root,
                commit=restore_commit,
                timeout=max(120, test_timeout),
                skip_git_clean_fdX=_skip_git_clean_fdX_for_task(task_dir),
            )
            result["after_restore"] = restore_result
            if not restore_result["ok"]:
                result["status"] = "fail_reset_workspace"
                result["failure_stage"] = "reset_workspace"
                return result
            result["status"] = "fail_patch_apply"
            result["failure_stage"] = "apply_patch"
            return result

    stage_result = stage_pr_tests(container, repo_root, pr_num, test_files)
    if not stage_result["ok"]:
        if restore_commit is not None:
            restore_result = _reset_repo_to_commit(
                container,
                repo_root,
                commit=restore_commit,
                timeout=max(120, test_timeout),
                skip_git_clean_fdX=_skip_git_clean_fdX_for_task(task_dir),
            )
            result["after_restore"] = restore_result
            if not restore_result["ok"]:
                result["status"] = "fail_reset_workspace"
                result["failure_stage"] = "reset_workspace"
                result["after"] = stage_result
                return result
        result["status"] = "fail_stage_tests"
        result["failure_stage"] = "after_tests"
        result["after"] = stage_result
        return result

    try:
        if lang == "java" and explicit_selected_tests is not None:
            after_mask = mask_unselected_java_tests(container, repo_root, pr_num, explicit_selected_tests)
            result["after_mask"] = after_mask
            if not after_mask["ok"]:
                result["status"] = "fail_after_mask"
                result["failure_stage"] = "after_tests"
                result["after"] = after_mask
                return result
        if lang == "java":
            cleanup_test_files = explicit_selected_tests if explicit_selected_tests is not None else test_files
            after_cleanup = clean_java_build_outputs(container, repo_root, cleanup_test_files)
            result["after_cleanup"] = after_cleanup
            if not after_cleanup["ok"]:
                result["status"] = "fail_after_cleanup"
                result["failure_stage"] = "after_tests"
                result["after"] = after_cleanup
                return result
        after = run_pr_tests(container, lang, repo_id, repo_root, task_dir, pr_num, test_files, test_timeout, test_labels=test_labels)
        result["after"] = after
        if not after["passed"]:
            result["status"] = "fail_after_not_passing"
            result["failure_stage"] = "after_tests"
            attach_failure_classification(result, after)
            return result
    finally:
        if lang == "java" and explicit_selected_tests is not None:
            restore_masked_java_tests(container, repo_root, pr_num)
        restore_pr_tests(container, repo_root, pr_num)
        if restore_commit is not None:
            restore_result = _reset_repo_to_commit(
                container,
                repo_root,
                commit=restore_commit,
                timeout=max(120, test_timeout),
                skip_git_clean_fdX=_skip_git_clean_fdX_for_task(task_dir),
            )
            result["after_restore"] = restore_result
            if not restore_result["ok"] and result["status"] in {"pass", "p2p", "fail_after_not_passing"}:
                result["status"] = "fail_reset_workspace"
                result["failure_stage"] = "reset_workspace"

    # Even pass-to-pass units must execute the post-patch run; we only label the
    # result as P2P after the current patch has been applied and the after run passes.
    if before_passed:
        result["status"] = "p2p"
    return result


def validate_tested_pr_strict(container: str, repo_root: str, lang: str, repo_id: str, task_dir: Path, pr_num: str, test_timeout: int) -> dict:
    return validate_tested_pr_strict_cumulative(
        container=container,
        repo_root=repo_root,
        lang=lang,
        repo_id=repo_id,
        task_dir=task_dir,
        pr_num=pr_num,
        test_timeout=test_timeout,
    )


def _run_strict_pr_sequence_cumulative(
    container_name: str,
    repo_root: str,
    lang: str,
    rid: str,
    task_dir: Path,
    pr_order: list[str],
    tested_prs: set[str],
    test_timeout: int,
    *,
    stop_on_fail: bool = False,
    start_pr: str | None = None,
    on_result=None,
) -> tuple[list[dict], dict[str, int], bool]:
    prs: list[dict] = []
    summary = {
        "strict_pass": 0,
        "strict_fail": 0,
        "skipped_no_tests": 0,
        "patch_apply_only": 0,
    }
    overall_ok = True
    skip_until = start_pr
    patch_cwd = infer_patch_apply_cwd(task_dir, repo_root)
    apply_timeout = max(120, test_timeout)

    for i, pr_num in enumerate(pr_order):
        prefix = f"[{i + 1}/{len(pr_order)}] PR #{pr_num}"

        if skip_until and pr_num != skip_until:
            patch_host_path = resolve_patch_host_path(task_dir, pr_num)
            pr_result = {
                "pr": pr_num,
                "tested": pr_num in tested_prs,
                "status": "skip_before_target",
                "patch_host_path": str(patch_host_path) if patch_host_path else None,
            }
            if patch_host_path is not None:
                pr_result["patch_apply"] = _apply_current_pr_patch(
                    container=container_name,
                    repo_root=repo_root,
                    repo_id=rid,
                    task_dir=task_dir,
                    pr_num=pr_num,
                    apply_timeout=apply_timeout,
                    patch_cwd=patch_cwd,
                )
            prs.append(pr_result)
            if on_result is not None:
                on_result(i, prefix, pr_result)
            continue
        skip_until = None

        if pr_num not in tested_prs:
            patch_host_path = resolve_patch_host_path(task_dir, pr_num)
            pr_result = {
                "pr": pr_num,
                "tested": False,
                "status": "pass" if patch_host_path else "fail_patch_missing",
                "failure_stage": None if patch_host_path else "apply_patch",
                "patch_host_path": str(patch_host_path) if patch_host_path else None,
            }
            if patch_host_path is None:
                overall_ok = False
                summary["strict_fail"] += 1
            else:
                pr_result["patch_apply"] = _apply_current_pr_patch(
                    container=container_name,
                    repo_root=repo_root,
                    repo_id=rid,
                    task_dir=task_dir,
                    pr_num=pr_num,
                    apply_timeout=apply_timeout,
                    patch_cwd=patch_cwd,
                )
                if pr_result["patch_apply"]["ok"]:
                    summary["patch_apply_only"] += 1
                else:
                    pr_result["status"] = "fail_patch_apply"
                    pr_result["failure_stage"] = "apply_patch"
                    overall_ok = False
                    summary["strict_fail"] += 1
            prs.append(pr_result)
            if on_result is not None:
                on_result(i, prefix, pr_result)
            if stop_on_fail and pr_result["status"].startswith("fail"):
                break
            continue

        pr_result = validate_tested_pr_strict_cumulative(
            container=container_name,
            repo_root=repo_root,
            lang=lang,
            repo_id=rid,
            task_dir=task_dir,
            pr_num=pr_num,
            test_timeout=test_timeout,
        )
        prs.append(pr_result)
        if pr_result["status"] == "pass":
            summary["strict_pass"] += 1
        elif pr_result["status"] == "p2p":
            summary["strict_pass"] += 1
        elif pr_result["status"] == "skip_no_tests":
            summary["skipped_no_tests"] += 1
        else:
            summary["strict_fail"] += 1
            overall_ok = False
        if on_result is not None:
            on_result(i, prefix, pr_result)
        if stop_on_fail and pr_result["status"].startswith("fail"):
            break

    return prs, summary, overall_ok


def write_json(path: Path | None, payload: dict):
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Per-PR cumulative validation")
    parser.add_argument("repo_id", nargs="?", help="Repository ID for seg tasks")
    parser.add_argument("--segment", type=int, nargs="+", help="Segment number(s)")
    parser.add_argument("--task-dir", help="Standalone or seg task directory path")
    parser.add_argument("--stop-on-fail", action="store_true", help="Stop at first failure")
    parser.add_argument("--pr", type=str, help="Start from specific PR number")
    parser.add_argument("--timeout", type=int, default=600, help="Docker build timeout")
    parser.add_argument("--test-timeout", type=int, default=None, help="Per-PR test timeout (defaults to task max_test_timeout_sec when present)")
    parser.add_argument("--strict-fail-to-pass", action="store_true", help="Require before-patch failure and after-patch success for every tested PR")
    parser.add_argument("--json-out", help="Write structured results JSON to this path")
    args = parser.parse_args()

    any_failures = False
    all_results: list[dict] = []

    if args.task_dir:
        task_dir = Path(args.task_dir).resolve()
        print(f"\n=== {task_dir.name} ===")
        result = run_task_dir(task_dir, args)
        all_results.append(result)
        any_failures = not result["overall_ok"]
    else:
        if not args.repo_id or not args.segment:
            parser.error("Provide repo_id with --segment, or use --task-dir")
        for seg_num in args.segment:
            print(f"\n=== {args.repo_id} seg{seg_num} ===")
            result = run_task_dir(find_task_dir(args.repo_id, seg_num), args, repo_id=args.repo_id)
            all_results.append(result)
            if not result["overall_ok"]:
                any_failures = True

    json_path = Path(args.json_out).resolve() if args.json_out else None
    payload = all_results[0] if len(all_results) == 1 else {"results": all_results, "overall_ok": not any_failures}
    write_json(json_path, payload)

    if any_failures:
        sys.exit(1)


def run_task_dir(task_dir: Path, args, repo_id: str | None = None) -> dict:
    task_name = task_dir.name.lower()
    run_suffix = hashlib.sha1(f"{task_name}-{os.getpid()}-{time.time_ns()}".encode()).hexdigest()[:10]
    container_name = f"lhb-val-{task_name}-{run_suffix}-client"
    tester_name = f"lhb-val-{task_name}-{run_suffix}-tester"
    project_name = f"lhb-val-{task_name}-{run_suffix}"

    rid = infer_repo_id(task_dir, fallback_repo_id=extract_repo_id(task_dir.name) or repo_id or task_dir.name.removeprefix("task_"))
    seg_id = extract_segment_id(task_dir.name)
    pr_order = get_pr_order(task_dir)
    tested_prs = get_tested_prs(task_dir)
    lang = infer_language(task_dir, fallback_repo_id=rid)
    repo_root = infer_repo_root(task_dir)
    args.test_timeout = get_task_test_timeout(task_dir, args.test_timeout)

    result = {
        "task_name": task_dir.name,
        "repo_id": rid,
        "language": lang,
        "repo_root": repo_root,
        "segment": seg_id,
        "strict_fail_to_pass": bool(args.strict_fail_to_pass),
        "docker": {"build_ok": False, "container_ok": False, "git_init_ok": False, "git_available": None},
        "summary": {
            "total_prs": len(pr_order),
            "tested_prs_total": len(tested_prs),
            "strict_pass": 0,
            "strict_fail": 0,
            "skipped_no_tests": 0,
            "patch_apply_only": 0,
        },
        "prs": [],
        "overall_ok": True,
        "failure_stage": None,
    }

    print(f"{_c(BOLD, 'Task')}: {task_dir.name}")
    print(f"{_c(BOLD, 'Repo')}: {rid}")
    print(f"{_c(BOLD, 'Total PRs')}: {len(pr_order)}")
    print(f"{_c(BOLD, 'Tested PRs')}: {len(tested_prs)}")
    print()

    env = docker_env()
    env["LHB_TASK_DOCKER_CLIENT_IMAGE_NAME"] = f"lhb-val-{task_name}-{run_suffix}"
    env["LHB_TASK_DOCKER_CLIENT_CONTAINER_NAME"] = container_name
    env["LHB_TASK_DOCKER_TESTER_IMAGE_NAME"] = f"lhb-val-{task_name}-{run_suffix}"
    env["LHB_TASK_DOCKER_TESTER_CONTAINER_NAME"] = tester_name
    env["LHB_TEST_DIR"] = "/tests"
    env["LHB_CONTAINER_LOGS_PATH"] = "/logs"
    env["LHB_CONTAINER_AGENT_LOGS_PATH"] = "/agent-logs"
    HOST_LOGS_PATH.mkdir(parents=True, exist_ok=True)
    HOST_AGENT_LOGS_PATH.mkdir(parents=True, exist_ok=True)
    env["LHB_TASK_LOGS_PATH"] = str(HOST_LOGS_PATH)
    env["LHB_TASK_AGENT_LOGS_PATH"] = str(HOST_AGENT_LOGS_PATH)

    subprocess.run(docker_cmd("compose", "-f", str(task_dir / "docker-compose.yaml"), "-p", project_name, "down", "--remove-orphans"), env=env, capture_output=True, text=True, timeout=60, cwd=task_dir)
    subprocess.run(docker_cmd("rm", "-f", container_name, tester_name), env=env, capture_output=True, text=True, timeout=60)

    print("Building Docker image...", end="", flush=True)
    build = subprocess.run(
        docker_cmd("compose", "-f", str(task_dir / "docker-compose.yaml"), "-p", project_name, "build"),
        env=env,
        capture_output=True,
        text=True,
        timeout=args.timeout,
        cwd=task_dir,
    )
    if build.returncode != 0:
        print(f" {_c(RED, 'FAIL')}")
        result["docker"]["build_stdout"] = _tail(build.stdout)
        result["docker"]["build_stderr"] = _tail(build.stderr)
        result["overall_ok"] = False
        result["failure_stage"] = "build"
        print(_tail(build.stderr or build.stdout, 500))
        return result
    result["docker"]["build_ok"] = True
    print(f" {_c(GREEN, 'OK')}")

    try:
        print("Starting container...", end="", flush=True)
        up = subprocess.run(
            docker_cmd("compose", "-f", str(task_dir / "docker-compose.yaml"), "-p", project_name, "up", "-d"),
            env=env,
            capture_output=True,
            text=True,
            timeout=args.timeout,
            cwd=task_dir,
        )
        if up.returncode != 0:
            print(f" {_c(RED, 'FAIL')}")
            result["docker"]["up_stdout"] = _tail(up.stdout)
            result["docker"]["up_stderr"] = _tail(up.stderr)
            result["overall_ok"] = False
            result["failure_stage"] = "container"
            return result

        for _ in range(10):
            check = subprocess.run(
                docker_cmd("inspect", "-f", "{{.State.Running}}", container_name),
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
            )
            if check.returncode == 0 and "true" in check.stdout.lower():
                break
            time.sleep(1)
        else:
            print(f" {_c(RED, 'FAIL')}")
            result["overall_ok"] = False
            result["failure_stage"] = "container"
            result["docker"]["inspect_stdout"] = _tail(check.stdout)
            result["docker"]["inspect_stderr"] = _tail(check.stderr)
            return result
        result["docker"]["container_ok"] = True
        print(f" {_c(GREEN, 'OK')}")

        print("Preparing workspace...", end="", flush=True)
        git_available, git_install = ensure_git_available(container_name)
        result["docker"]["git_available"] = git_available
        if git_install.get("installed"):
            result["docker"]["git_install"] = git_install
        init_steps = [
            docker_exec(container_name, ["git", "init", "/workspace"]),
            docker_exec(container_name, ["git", "-C", "/workspace", "config", "user.name", "LHB Validator"]),
            docker_exec(container_name, ["git", "-C", "/workspace", "config", "user.email", "lhb-validator@example.invalid"]),
            docker_exec(container_name, ["git", "-C", "/workspace", "add", "-A"]),
            docker_exec(container_name, ["git", "-C", "/workspace", "commit", "-m", "base", "--allow-empty"]),
            ensure_workspace_dirs(container_name),
        ]
        if (not git_available) or any(rc != 0 for rc, _, _ in init_steps):
            print(f" {_c(RED, 'FAIL')}")
            result["overall_ok"] = False
            result["failure_stage"] = "git_init"
            result["docker"]["git_init"] = [{"returncode": rc, "stdout": _tail(out), "stderr": _tail(err)} for rc, out, err in init_steps]
            if not git_available:
                result["docker"]["git_install"] = git_install
            return result
        result["docker"]["git_init_ok"] = True
        print(f" {_c(GREEN, 'OK')}")

        print("Copying tests to container...", end="", flush=True)
        copy_tests = copy_tree_to_container(task_dir / "tests", container_name, "/tests")
        if copy_tests[0] != 0:
            print(f" {_c(RED, 'FAIL')}")
            result["overall_ok"] = False
            result["failure_stage"] = "copy_tests"
            result["docker"]["copy_tests"] = {"returncode": copy_tests[0], "stdout": _tail(copy_tests[1]), "stderr": _tail(copy_tests[2])}
            return result
        dag_path = task_dir / "unit_dag.json"
        if dag_path.is_file():
            copy_dag = copy_host_path_to_container(dag_path, container_name, "/tests/unit_dag.json")
            if copy_dag[0] != 0:
                print(f" {_c(RED, 'FAIL')}")
                result["overall_ok"] = False
                result["failure_stage"] = "copy_tests"
                result["docker"]["copy_unit_dag"] = {
                    "returncode": copy_dag[0],
                    "stdout": _tail(copy_dag[1]),
                    "stderr": _tail(copy_dag[2]),
                }
                return result
        print(f" {_c(GREEN, 'OK')}")
        print()

        solution = task_dir / "solution.sh"
        run_tests = task_dir / "run-tests.sh"
        is_task_level = is_task_level_strict_task(task_dir)
        if is_task_level:
            print(f"  {_c(BOLD, 'TEST')}  [1/1] task-level strict validation...", end="", flush=True)
            pr_result = validate_task_strict(container_name, repo_root, task_dir, args.test_timeout)
            result["prs"].append(pr_result)
            result["summary"]["total_prs"] = 1
            result["summary"]["tested_prs_total"] = 1
            if pr_result["status"] == "pass":
                result["summary"]["strict_pass"] = 1
                print(f" {_c(GREEN, 'PASS')}")
            else:
                result["summary"]["strict_fail"] = 1
                result["overall_ok"] = False
                print(f" {_c(RED, 'FAIL')} ({pr_result['status']})")
                if pr_result.get("before") and pr_result["before"].get("stdout"):
                    print(f"       before stdout: {pr_result['before']['stdout'][-300:]}")
                if pr_result.get("before") and pr_result["before"].get("stderr"):
                    print(f"       before stderr: {pr_result['before']['stderr'][-300:]}")
                if pr_result.get("patch_apply") and pr_result["patch_apply"].get("stderr"):
                    print(f"       patch stderr: {pr_result['patch_apply']['stderr'][-300:]}")
                if pr_result.get("after") and pr_result["after"].get("stdout"):
                    print(f"       after stdout: {pr_result['after']['stdout'][-300:]}")
                if pr_result.get("after") and pr_result["after"].get("stderr"):
                    print(f"       after stderr: {pr_result['after']['stderr'][-300:]}")
        else:
            reset_once = reset_repo_workspace(
                container_name,
                repo_root,
                timeout=max(120, args.test_timeout),
                skip_git_clean_fdX=_skip_git_clean_fdX_for_task(task_dir),
            )
            result["workspace_reset_before_sequence"] = reset_once
            if not reset_once["ok"]:
                result["overall_ok"] = False
                result["failure_stage"] = "reset_workspace"
                return result

            assets = copy_task_runtime_assets(container_name, task_dir, repo_root)
            result["runtime_assets"] = assets
            if not assets["ok"]:
                result["overall_ok"] = False
                result["failure_stage"] = "stage_runtime"
                return result

            def _report_pr_result(_index: int, prefix: str, pr_result: dict) -> None:
                if pr_result["status"] == "skip_before_target":
                    print(f"  {_c(YELLOW, 'SKIP')} {prefix} (before --pr {args.pr})")
                    return
                if pr_result["tested"] is False:
                    if pr_result["status"] == "skip_no_tests":
                        print(f"  {_c(YELLOW, 'SKIP')} {prefix}")
                        return
                    if pr_result["status"] == "pass":
                        print(f"  {_c(CYAN, 'APPLY')} {prefix} (no tests)")
                    elif pr_result["status"] == "fail_patch_missing":
                        print(f"  {_c(RED, 'FAIL')} {prefix} (missing patch)")
                    elif pr_result["status"] == "fail_patch_apply":
                        print(f"  {_c(RED, 'FAIL')} {prefix} (patch apply)")
                    else:
                        print(f"  {_c(RED, 'FAIL')} {prefix} ({pr_result['status']})")
                    return

                print(f"  {_c(BOLD, 'TEST')}  {prefix}...", end="", flush=True)
                if pr_result["status"] == "pass":
                    print(f" {_c(GREEN, 'PASS')}")
                elif pr_result["status"] == "p2p":
                    print(f" {_c(YELLOW, 'P2P')}")
                elif pr_result["status"] == "skip_no_tests":
                    print(f" {_c(YELLOW, 'SKIP')}")
                else:
                    print(f" {_c(RED, 'FAIL')} ({pr_result['status']})")
                    if pr_result.get("before"):
                        before = pr_result["before"]
                        if before.get("stdout"):
                            print(f"       before stdout: {before['stdout'][-300:]}")
                        if before.get("stderr"):
                            print(f"       before stderr: {before['stderr'][-300:]}")
                    if pr_result.get("patch_apply") and pr_result["patch_apply"].get("stderr"):
                        print(f"       patch stderr: {pr_result['patch_apply']['stderr'][-300:]}")
                    if pr_result.get("after"):
                        after = pr_result["after"]
                        if after.get("stdout"):
                            print(f"       after stdout: {after['stdout'][-300:]}")
                        if after.get("stderr"):
                            print(f"       after stderr: {after['stderr'][-300:]}")

            seq_prs, seq_summary, seq_overall_ok = _run_strict_pr_sequence_cumulative(
                container_name=container_name,
                repo_root=repo_root,
                lang=lang,
                rid=rid,
                task_dir=task_dir,
                pr_order=pr_order,
                tested_prs=tested_prs,
                test_timeout=args.test_timeout,
                stop_on_fail=args.stop_on_fail,
                start_pr=args.pr,
                on_result=_report_pr_result,
            )
            result["prs"].extend(seq_prs)
            result["summary"]["strict_pass"] += seq_summary["strict_pass"]
            result["summary"]["strict_fail"] += seq_summary["strict_fail"]
            result["summary"]["skipped_no_tests"] += seq_summary["skipped_no_tests"]
            result["summary"]["patch_apply_only"] += seq_summary["patch_apply_only"]
            if not seq_overall_ok:
                result["overall_ok"] = False
                if args.stop_on_fail and result["summary"]["strict_fail"] > 0:
                    print(f"\n{_c(RED, 'Stopped at first failure.')}")
                    print(f"Container still running: {container_name}")
                    print(f"To debug: docker exec -it {container_name} bash")

        print(
            f"\n{_c(BOLD, 'Summary')}: "
            f"{result['summary']['strict_pass']} strict pass, "
            f"{result['summary']['strict_fail']} strict fail, "
            f"{result['summary']['patch_apply_only']} patch-only, "
            f"{result['summary']['skipped_no_tests']} skipped / "
            f"{result['summary']['total_prs']} total"
        )
        return result

    finally:
        if not args.stop_on_fail or result["overall_ok"]:
            subprocess.run(
                docker_cmd("compose", "-f", str(task_dir / "docker-compose.yaml"), "-p", project_name, "down"),
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=task_dir,
            )


if __name__ == "__main__":
    main()
