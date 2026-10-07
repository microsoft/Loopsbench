from __future__ import annotations

import json
import subprocess
import sys
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from loopsbench.handlers.trial_handler import Task, TaskDifficulty
from loopsbench.utils.compose_security import validate_compose_security

REQUIRED_CONTRIBUTION_FILES = (
    "task.yaml",
    "Dockerfile",
    "docker-compose.yaml",
    "run-tests.sh",
    "unit_dag.json",
    "module_dag.yaml",
    "slug_diff_map.json",
)

REQUIRED_PROVENANCE_FIELDS = (
    "source_url",
    "source_repository_url",
    "source_base_revision",
    "proposal_url",
    "license_status",
)

SENSITIVE_BASE_NAMES = {
    "tests",
    "gold_patches",
    "solution.sh",
    "solution.yaml",
    "run-tests.sh",
    "gold-patch.diff",
    "gold_patch.diff",
}


@dataclass
class ValidationIssue:
    code: str
    message: str
    path: str | None = None


@dataclass
class CommandResult:
    name: str
    command: list[str]
    returncode: int
    stdout_tail: str
    stderr_tail: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class TaskContributionReport:
    task_id: str
    task_dir: str
    issues: list[ValidationIssue] = field(default_factory=list)
    commands: list[CommandResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues and all(command.ok for command in self.commands)

    def add_issue(self, code: str, message: str, *, path: Path | None = None) -> None:
        self.issues.append(
            ValidationIssue(
                code=code,
                message=message,
                path=str(path) if path is not None else None,
            )
        )

    def add_command(self, result: CommandResult) -> None:
        self.commands.append(result)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ok"] = self.ok
        return payload


def _tail(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def _resolve_within(root: Path, relative_path: str) -> Path | None:
    relative = Path(relative_path)
    if relative.is_absolute():
        return None
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def missing_provenance_fields(raw_payload: dict[str, Any]) -> list[str]:
    return [
        field_name
        for field_name in REQUIRED_PROVENANCE_FIELDS
        if not str(raw_payload.get(field_name) or "").strip()
    ]


def _validate_acyclic(
    *,
    node_ids: list[str],
    edges: list[dict[str, Any]],
    report: TaskContributionReport,
    code_prefix: str,
    path: Path,
) -> None:
    indegree: dict[str, int] = {node_id: 0 for node_id in node_ids}
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        src = str(edge.get("from", "")).strip()
        dst = str(edge.get("to", "")).strip()
        if src in indegree and dst in indegree:
            adjacency[src].append(dst)
            indegree[dst] += 1

    frontier = deque(
        sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    )
    visited = 0
    while frontier:
        current = frontier.popleft()
        visited += 1
        for nxt in adjacency[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                frontier.append(nxt)

    if visited != len(node_ids):
        report.add_issue(
            f"{code_prefix}_cyclic_graph",
            "Dependency graph must be acyclic.",
            path=path,
        )


def _validate_task_yaml(
    task_dir: Path,
    report: TaskContributionReport,
    *,
    require_provenance: bool = False,
) -> Path | None:
    task_yaml = task_dir / "task.yaml"
    try:
        raw_payload = yaml.safe_load(task_yaml.read_text(encoding="utf-8")) or {}
        task = Task.model_validate(raw_payload)
    except Exception as exc:  # noqa: BLE001
        report.add_issue(
            "invalid_task_yaml",
            f"task.yaml failed schema validation: {exc}",
            path=task_yaml,
        )
        return None

    if not task.instruction.strip():
        report.add_issue(
            "missing_instruction",
            "task.yaml instruction must not be empty.",
            path=task_yaml,
        )
    if task.author_name.strip().lower() == "unknown":
        report.add_issue(
            "missing_author_name", "task.yaml author_name must be set.", path=task_yaml
        )
    if task.author_email.strip().lower() == "unknown":
        report.add_issue(
            "missing_author_email",
            "task.yaml author_email must be set.",
            path=task_yaml,
        )
    if task.difficulty == TaskDifficulty.UNKNOWN:
        report.add_issue(
            "invalid_difficulty",
            "task.yaml difficulty must be a concrete value.",
            path=task_yaml,
        )
    if not task.parser_name:
        report.add_issue(
            "missing_parser_name", "task.yaml parser_name must be set.", path=task_yaml
        )

    compose_file = Path(task.docker.compose_file)
    compose_path: Path | None = None
    if compose_file.is_absolute() or compose_file.name == "":
        report.add_issue(
            "invalid_compose_path",
            "task.yaml docker.compose_file must be a relative file path.",
            path=task_yaml,
        )
    else:
        compose_path = _resolve_within(task_dir, str(compose_file))
    if compose_path is None and not (
        compose_file.is_absolute() or compose_file.name == ""
    ):
        report.add_issue(
            "compose_path_traversal",
            "task.yaml docker.compose_file must stay inside the task directory.",
            path=task_yaml,
        )
    elif compose_path is not None and not compose_path.is_file():
        report.add_issue(
            "missing_compose_file",
            f"Missing docker compose file `{task.docker.compose_file}`.",
            path=compose_path,
        )

    missing_fields = missing_provenance_fields(raw_payload)
    if require_provenance and missing_fields:
        for key in missing_fields:
            report.add_issue(
                "missing_publish_provenance_field",
                f"Publish validation requires `{key}` to be set in task.yaml.",
                path=task_yaml,
            )
    elif missing_fields and len(missing_fields) != len(REQUIRED_PROVENANCE_FIELDS):
        for key in missing_fields:
            report.add_issue(
                "missing_provenance_field",
                f"task.yaml includes provenance metadata, so `{key}` must also be set.",
                path=task_yaml,
            )
    return compose_path


def _validate_compose_file(
    compose_path: Path | None, report: TaskContributionReport
) -> None:
    if compose_path is None or not compose_path.is_file():
        return
    for issue in validate_compose_security(compose_path):
        report.add_issue(issue.code, issue.message, path=issue.path)


def _validate_unit_dag(task_dir: Path, report: TaskContributionReport) -> list[str]:
    unit_dag_path = task_dir / "unit_dag.json"
    try:
        payload = _load_json(unit_dag_path)
    except Exception as exc:  # noqa: BLE001
        report.add_issue(
            "invalid_unit_dag",
            f"unit_dag.json is not valid JSON: {exc}",
            path=unit_dag_path,
        )
        return []

    nodes = payload.get("nodes")
    edges = payload.get("edges", [])
    if not isinstance(nodes, list) or not nodes:
        report.add_issue(
            "missing_unit_nodes",
            "unit_dag.json must define a non-empty nodes list.",
            path=unit_dag_path,
        )
        return []
    if not isinstance(edges, list):
        report.add_issue(
            "invalid_unit_edges",
            "unit_dag.json edges must be a list.",
            path=unit_dag_path,
        )
        edges = []

    node_ids: list[str] = []
    seen_ids: set[str] = set()
    layers: set[int] = set()
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            report.add_issue(
                "invalid_unit_node",
                f"unit_dag.json node #{index} must be an object.",
                path=unit_dag_path,
            )
            continue
        node_id = str(node.get("id", "")).strip()
        if not node_id:
            report.add_issue(
                "missing_unit_id",
                f"unit_dag.json node #{index} is missing id.",
                path=unit_dag_path,
            )
            continue
        if node_id in seen_ids:
            report.add_issue(
                "duplicate_unit_id",
                f"Duplicate unit id `{node_id}` in unit_dag.json.",
                path=unit_dag_path,
            )
            continue
        seen_ids.add(node_id)
        node_ids.append(node_id)
        layer = node.get("layer")
        if not isinstance(layer, int):
            report.add_issue(
                "missing_unit_layer",
                f"Unit `{node_id}` must define an integer layer.",
                path=unit_dag_path,
            )
        else:
            layers.add(layer)

    for edge in edges:
        if not isinstance(edge, dict):
            report.add_issue(
                "invalid_unit_edge",
                "Each unit DAG edge must be an object.",
                path=unit_dag_path,
            )
            continue
        src = str(edge.get("from", "")).strip()
        dst = str(edge.get("to", "")).strip()
        if src not in seen_ids or dst not in seen_ids:
            report.add_issue(
                "invalid_prerequisite",
                f"Unit DAG edge `{src} -> {dst}` must reference existing units.",
                path=unit_dag_path,
            )
        if src == dst and src:
            report.add_issue(
                "self_cycle",
                f"Unit DAG edge `{src} -> {dst}` forms a self-cycle.",
                path=unit_dag_path,
            )

    expected_total_units = payload.get("total_units")
    if isinstance(expected_total_units, int) and expected_total_units != len(node_ids):
        report.add_issue(
            "unit_count_mismatch",
            f"unit_dag.json total_units={expected_total_units} does not match nodes={len(node_ids)}.",
            path=unit_dag_path,
        )

    expected_num_layers = payload.get("num_layers")
    if isinstance(expected_num_layers, int) and expected_num_layers != len(layers):
        report.add_issue(
            "unit_layer_count_mismatch",
            f"unit_dag.json num_layers={expected_num_layers} does not match discovered layers={len(layers)}.",
            path=unit_dag_path,
        )

    _validate_acyclic(
        node_ids=node_ids,
        edges=[edge for edge in edges if isinstance(edge, dict)],
        report=report,
        code_prefix="unit_dag",
        path=unit_dag_path,
    )
    return node_ids


def _validate_module_dag(task_dir: Path, report: TaskContributionReport) -> None:
    module_dag_path = task_dir / "module_dag.yaml"
    try:
        payload = _load_yaml(module_dag_path)
    except Exception as exc:  # noqa: BLE001
        report.add_issue(
            "invalid_module_dag",
            f"module_dag.yaml is not valid YAML: {exc}",
            path=module_dag_path,
        )
        return

    nodes = payload.get("nodes")
    edges = payload.get("edges", [])
    if not isinstance(nodes, list) or not nodes:
        report.add_issue(
            "missing_module_nodes",
            "module_dag.yaml must define a non-empty nodes list.",
            path=module_dag_path,
        )
        return

    module_ids: list[str] = []
    seen_ids: set[str] = set()
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            report.add_issue(
                "invalid_module_node",
                f"module_dag.yaml node #{index} must be an object.",
                path=module_dag_path,
            )
            continue
        node_id = str(node.get("id", "")).strip()
        if not node_id:
            report.add_issue(
                "missing_module_id",
                f"module_dag.yaml node #{index} is missing id.",
                path=module_dag_path,
            )
            continue
        if node_id in seen_ids:
            report.add_issue(
                "duplicate_module_id",
                f"Duplicate module id `{node_id}` in module_dag.yaml.",
                path=module_dag_path,
            )
            continue
        seen_ids.add(node_id)
        module_ids.append(node_id)

    for edge in edges if isinstance(edges, list) else []:
        if not isinstance(edge, dict):
            report.add_issue(
                "invalid_module_edge",
                "Each module DAG edge must be an object.",
                path=module_dag_path,
            )
            continue
        src = str(edge.get("from", "")).strip()
        dst = str(edge.get("to", "")).strip()
        if src not in seen_ids or dst not in seen_ids:
            report.add_issue(
                "invalid_module_dependency",
                f"Module DAG edge `{src} -> {dst}` must reference existing modules.",
                path=module_dag_path,
            )

    _validate_acyclic(
        node_ids=module_ids,
        edges=[edge for edge in edges if isinstance(edge, dict)]
        if isinstance(edges, list)
        else [],
        report=report,
        code_prefix="module_dag",
        path=module_dag_path,
    )


def _validate_requirements(
    task_dir: Path, unit_ids: list[str], report: TaskContributionReport
) -> list[str]:
    requirements_dir = task_dir / "requirements"
    requirement_paths = sorted(requirements_dir.glob("*.yaml"))
    if not requirement_paths:
        report.add_issue(
            "missing_requirements",
            "requirements/ must contain one YAML file per unit.",
            path=requirements_dir,
        )
        return []

    requirement_stems: list[str] = []
    for path in requirement_paths:
        try:
            payload = _load_yaml(path) or {}
        except Exception as exc:  # noqa: BLE001
            report.add_issue(
                "invalid_requirement_yaml",
                f"{path.name} is not valid YAML: {exc}",
                path=path,
            )
            continue
        requirement_id = str(payload.get("id", "")).strip()
        if not requirement_id:
            report.add_issue(
                "missing_requirement_id", f"{path.name} must define `id`.", path=path
            )
            continue
        if requirement_id != path.stem:
            report.add_issue(
                "requirement_id_mismatch",
                f"{path.name} has id `{requirement_id}` but file stem is `{path.stem}`.",
                path=path,
            )
        for field_name in ("title", "requirement"):
            if not str(payload.get(field_name, "")).strip():
                report.add_issue(
                    "missing_requirement_field",
                    f"{path.name} must define `{field_name}`.",
                    path=path,
                )
        requirement_stems.append(path.stem)

    if unit_ids:
        missing_requirements = sorted(set(unit_ids) - set(requirement_stems))
        extra_requirements = sorted(set(requirement_stems) - set(unit_ids))
        for unit_id in missing_requirements:
            report.add_issue(
                "missing_requirement_file",
                f"Unit `{unit_id}` is missing requirements/{unit_id}.yaml.",
                path=requirements_dir,
            )
        for unit_id in extra_requirements:
            report.add_issue(
                "orphan_requirement_file",
                f"requirements/{unit_id}.yaml does not match any unit in unit_dag.json.",
                path=requirements_dir,
            )
    return requirement_stems


def _load_noop_stems(gold_dir: Path) -> set[str]:
    # Keep the noop manifest name aligned with the checked-in task examples.
    noop_path = gold_dir / ".loopsbench_split_noop.json"
    if not noop_path.is_file():
        return set()
    try:
        payload = _load_json(noop_path)
    except Exception:
        return set()
    values = payload.get("noop", []) if isinstance(payload, dict) else []
    return {str(value).strip() for value in values if str(value).strip()}


def _validate_gold_paths(
    task_dir: Path,
    unit_ids: list[str],
    requirement_stems: list[str],
    report: TaskContributionReport,
) -> None:
    gold_dir = task_dir / "gold_patches"
    monolithic_candidates = [task_dir / "gold-patch.diff", task_dir / "gold_patch.diff"]
    has_monolithic = any(
        path.is_file() and path.stat().st_size > 0 for path in monolithic_candidates
    )
    has_gold_dir = gold_dir.is_dir()
    if not has_gold_dir and not has_monolithic:
        report.add_issue(
            "missing_gold_patch",
            "Task must include gold_patches/ or a monolithic gold-patch.diff file.",
            path=task_dir,
        )
        return

    slug_map_path = task_dir / "slug_diff_map.json"
    try:
        slug_map = _load_json(slug_map_path)
    except Exception as exc:  # noqa: BLE001
        report.add_issue(
            "invalid_slug_diff_map",
            f"slug_diff_map.json is not valid JSON: {exc}",
            path=slug_map_path,
        )
        return

    if not isinstance(slug_map, dict) or not slug_map:
        report.add_issue(
            "empty_slug_diff_map",
            "slug_diff_map.json must map requirement files to diff paths.",
            path=slug_map_path,
        )
        return

    expected_keys = {f"{stem}.yaml" for stem in requirement_stems}
    missing_keys = sorted(expected_keys - set(slug_map))
    extra_keys = sorted(set(slug_map) - expected_keys)
    for key in missing_keys:
        report.add_issue(
            "missing_slug_mapping",
            f"slug_diff_map.json is missing `{key}`.",
            path=slug_map_path,
        )
    for key in extra_keys:
        report.add_issue(
            "orphan_slug_mapping",
            f"slug_diff_map.json includes unknown requirement `{key}`.",
            path=slug_map_path,
        )

    diff_stems = {path.stem.removeprefix("pr_") for path in gold_dir.glob("*.diff")}
    noop_stems = _load_noop_stems(gold_dir) if has_gold_dir else set()
    for key, relative_path in slug_map.items():
        if not isinstance(relative_path, str) or not relative_path.strip():
            report.add_issue(
                "invalid_slug_mapping_value",
                f"slug_diff_map.json entry `{key}` must map to a path string.",
                path=slug_map_path,
            )
            continue
        resolved = _resolve_within(task_dir, relative_path)
        if resolved is None:
            report.add_issue(
                "path_traversal",
                f"slug_diff_map.json entry `{key}` points outside the task directory.",
                path=slug_map_path,
            )
            continue
        if not resolved.exists():
            report.add_issue(
                "missing_gold_patch_file",
                f"slug_diff_map.json entry `{key}` points to missing file `{relative_path}`.",
                path=slug_map_path,
            )

    if has_gold_dir and unit_ids:
        missing_unit_diffs = sorted(set(unit_ids) - diff_stems - noop_stems)
        for unit_id in missing_unit_diffs:
            report.add_issue(
                "missing_unit_patch",
                f"Unit `{unit_id}` has no corresponding gold_patches/{unit_id}.diff shard.",
                path=gold_dir,
            )


def _validate_task_tree(task_dir: Path, report: TaskContributionReport) -> None:
    if not task_dir.is_dir():
        report.add_issue(
            "missing_task_dir", "Task directory does not exist.", path=task_dir
        )
        return

    if not task_dir.name.startswith("task_"):
        report.add_issue(
            "invalid_task_id",
            "Task directory name must start with `task_`.",
            path=task_dir,
        )

    for required_file in REQUIRED_CONTRIBUTION_FILES:
        if not (task_dir / required_file).exists():
            report.add_issue(
                "missing_required_file",
                f"Missing required file `{required_file}`.",
                path=task_dir / required_file,
            )

    if not (
        (task_dir / "solution.sh").is_file() or (task_dir / "solution.yaml").is_file()
    ):
        report.add_issue(
            "missing_solution",
            "Task must include solution.sh or solution.yaml.",
            path=task_dir,
        )

    tests_dir = task_dir / "tests"
    if not tests_dir.is_dir() or not any(
        path.is_file() for path in tests_dir.rglob("*")
    ):
        report.add_issue(
            "missing_tests",
            "tests/ must contain at least one test file.",
            path=tests_dir,
        )

    base_dir = task_dir / "base"
    if not base_dir.is_dir():
        report.add_issue(
            "missing_base",
            "Task must include a base/ workspace snapshot.",
            path=base_dir,
        )

    for path in task_dir.rglob("*"):
        if path.is_symlink():
            report.add_issue(
                "symlink_not_allowed",
                "Task directory must not contain symlinks.",
                path=path,
            )

    if base_dir.is_dir():
        for path in base_dir.rglob("*"):
            rel = path.relative_to(base_dir)
            if rel.parts and rel.parts[0] in SENSITIVE_BASE_NAMES:
                report.add_issue(
                    "exposed_hidden_tests"
                    if rel.parts[0] == "tests"
                    else "exposed_gold",
                    f"base/ must not contain agent-visible `{rel.parts[0]}` content.",
                    path=path,
                )
            if path.name in SENSITIVE_BASE_NAMES:
                report.add_issue(
                    "exposed_hidden_tests" if path.name == "tests" else "exposed_gold",
                    f"base/ must not contain agent-visible `{path.name}` content.",
                    path=path,
                )


def validate_task_contribution(
    task_dir: Path, *, require_provenance: bool = False
) -> TaskContributionReport:
    resolved_task_dir = task_dir.resolve()
    report = TaskContributionReport(
        task_id=resolved_task_dir.name,
        task_dir=str(resolved_task_dir),
    )

    _validate_task_tree(resolved_task_dir, report)
    if any(issue.code == "missing_task_dir" for issue in report.issues):
        return report

    compose_path = _validate_task_yaml(
        resolved_task_dir, report, require_provenance=require_provenance
    )
    _validate_compose_file(compose_path, report)
    unit_ids = _validate_unit_dag(resolved_task_dir, report)
    _validate_module_dag(resolved_task_dir, report)
    requirement_stems = _validate_requirements(resolved_task_dir, unit_ids, report)
    _validate_gold_paths(resolved_task_dir, unit_ids, requirement_stems, report)
    return report


def run_command(name: str, command: list[str], *, cwd: Path) -> CommandResult:
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return CommandResult(
        name=name,
        command=command,
        returncode=completed.returncode,
        stdout_tail=_tail(completed.stdout),
        stderr_tail=_tail(completed.stderr),
    )


def run_task_contribution_checks(
    task_dir: Path,
    *,
    run_tasks_validate: bool = False,
    run_oracle: bool = False,
    oracle_output_root: Path | None = None,
    require_provenance: bool = False,
) -> TaskContributionReport:
    report = validate_task_contribution(task_dir, require_provenance=require_provenance)
    repo_root = Path(__file__).resolve().parents[1]
    task_id = task_dir.resolve().name
    tasks_root = task_dir.resolve().parent
    if report.issues:
        return report

    if run_tasks_validate:
        report.add_command(
            run_command(
                "loopsbench_tasks_validate",
                [
                    sys.executable,
                    "-m",
                    "loopsbench.cli.main",
                    "tasks",
                    "validate",
                    "--tasks-dir",
                    str(tasks_root),
                    "--task-id",
                    task_id,
                ],
                cwd=repo_root,
            )
        )

    if run_oracle:
        output_root = (
            oracle_output_root or (repo_root / "runs" / "contribution-oracle")
        ).resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        report.add_command(
            run_command(
                "oracle",
                [
                    sys.executable,
                    "-m",
                    "loopsbench.cli.main",
                    "run",
                    "--agent",
                    "oracle",
                    "--task-id",
                    task_id,
                    "--dataset-path",
                    str(tasks_root),
                    "--docker-image-strategy",
                    "local-build",
                    "--output-path",
                    str(output_root),
                ],
                cwd=repo_root,
            )
        )

    return report
