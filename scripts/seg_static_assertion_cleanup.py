from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
TASKS_ROOT = ROOT / "tasks"
DESIGN_SPEC = ROOT / "docs/superpowers/specs/2026-06-15-seg-static-assertion-cleanup-design.md"
SEG_SPEC = ROOT / "docs/seg-strengthening/seg-strengthening-spec.md"

PUBLIC_REQUIREMENT_HINTS = re.compile(
    r"\b("
    r"docs?|documentation|changelog|release|metadata|package|packaging|"
    r"dependency|dependencies|version(?:ing)?|workflow|pipeline|ci|"
    r"config|schema|defaults?|license|notice|entrypoint|cli|"
    r"type(?:s|\s+definition)?|typing|translation|language pack|locale|"
    r"i18n|template|markup|static asset|css|javascript|js"
    r")\b",
    re.I,
)
CONTENT_PAT = re.compile(r"lhb_content_check|test_content\.py|content_checks\.json|ContentMarker|test_marker\.py")
AST_PAT = re.compile(r"inspect\.getsource|ast\.(parse|walk|dump|NodeVisitor)")
READ_PAT = re.compile(
    r"read_text\(|read_bytes\(|os\.ReadFile\(|ioutil\.ReadFile\(|"
    r"Files\.read(AllBytes|String)\(|fs\.readFileSync\(|readFileSync\(|open\("
)
ASSERT_PAT = re.compile(
    r"containsString\(|ContainSubstring\(|assert\s+.*\s+in\s+|assertRegex\(|"
    r"assertIn\(|assertNotIn\(|assertThat\(|assert\.|strings\.Contains\(|"
    r"bytes\.Contains\(|t\.Fatalf?\(|self\.fail\("
)
GREP_PAT = re.compile(r"\b(grep|rg|sed -n|awk )\b")
STRING_PAT = re.compile(r"([\"'])([^\"'\n]{1,240})\1")
SPECIAL_PUBLIC_HELPERS = re.compile(r"locateReleaseConfig|_runtime_yml_path|translations?|locale|i18n|language|version_date", re.I)
SPECIAL_IMPL_HELPERS = re.compile(
    r"_ansible_src_root|repoPath\(|repo_path\(|readRepoFile\(|"
    r"inspect\.getsource|vault_mod\.__file__|formatterPath\("
)

IMPLEMENTATION_SEGMENTS = [
    "core/src/main/",
    "src/",
    "lib/",
    "server/",
    "django/",
    "ansible/",
    "module_utils/",
    "persistence/",
    "hudson/",
    "jenkins/",
    "ui/src/",
    "core/playback/",
    "queries/",
]
PUBLIC_SEGMENTS = [
    "changelog",
    "readme",
    "docs/",
    "version_date",
    ".tsv",
    "package.json",
    "go.mod",
    "go.sum",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock",
    "cargo.toml",
    "pom.xml",
    ".goreleaser.yml",
    ".github/",
    "workflow",
    "ci/",
    "dockerfile",
    "docker-compose",
    ".yaml",
    ".yml",
    "config/",
    "translations",
    "translation",
    "locale",
    "locales",
    "i18n",
    ".po",
    ".pot",
    "lang/",
    "languages/",
    "templates/",
]
PUBLIC_AMBIGUOUS_SEGMENTS = ["static/admin/js", "static/admin/css"]


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def task_dirs() -> list[Path]:
    return sorted(p for p in TASKS_ROOT.glob("task_*_seg*") if p.is_dir())


def is_test_entry(path: Path) -> bool:
    name = path.name
    if name in {"test_unit_runner.py", "test_task_runner.py"}:
        return False
    if path.suffix == ".py":
        return name.startswith("test") or name.endswith("_test.py")
    if path.suffix == ".go":
        return name.endswith("_test.go")
    if path.suffix == ".java":
        return name.endswith("Test.java")
    if path.suffix in {".js", ".ts"}:
        low = name.lower()
        return low.startswith("test") or ".test." in low or low.endswith("_test.js") or low.endswith("_test.ts")
    if path.suffix == ".rb":
        return name.endswith("_test.rb") or name.endswith("_spec.rb")
    if path.suffix == ".php":
        return name.endswith("Test.php")
    if path.suffix == ".sh":
        return name.startswith("test")
    return False


def pr_from_test_path(task_dir: Path, file_path: Path) -> str | None:
    rel = file_path.relative_to(task_dir / "tests")
    if rel.parts and re.fullmatch(r"\d+", rel.parts[0]):
        return rel.parts[0]
    if rel.parts and rel.parts[0].startswith("pr_") and rel.parts[0][3:].isdigit():
        return rel.parts[0][3:]
    return None


def load_rename_map(task_dir: Path) -> dict[str, str]:
    path = task_dir / "requirements" / ".rename_map.json"
    if not path.is_file():
        return {}
    try:
        data = read_json(path)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {key.replace(".yaml", ""): value for key, value in data.items()}


def load_slug_map(task_dir: Path) -> dict[str, str]:
    path = task_dir / "slug_diff_map.json"
    if not path.is_file():
        return {}
    try:
        data = read_json(path)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {Path(diff_rel).stem.removeprefix("pr_"): slug for slug, diff_rel in data.items()}


@dataclass
class RequirementMeta:
    file_name: str | None
    title: str | None
    text_blob: str
    lookup_source: str


def load_requirement_meta(task_dir: Path, pr: str | None, rename_map: dict[str, str], slug_map: dict[str, str]) -> RequirementMeta:
    if pr is None:
        return RequirementMeta(file_name=None, title=None, text_blob="", lookup_source="missing")

    req_name = rename_map.get(pr)
    lookup_source = "rename_map"
    if req_name is None:
        req_name = slug_map.get(pr)
        lookup_source = "slug_diff_map"
    if req_name is None:
        direct = task_dir / "requirements" / f"{pr}.yaml"
        if direct.is_file():
            req_name = direct.name
            lookup_source = "direct_pr_yaml"
    if req_name is None:
        return RequirementMeta(file_name=None, title=None, text_blob="", lookup_source="missing")

    req_path = task_dir / "requirements" / req_name
    if not req_path.is_file():
        return RequirementMeta(file_name=req_name, title=None, text_blob="", lookup_source=lookup_source)

    try:
        payload = yaml.safe_load(req_path.read_text()) or {}
    except Exception:
        payload = {}

    parts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            parts.append(node)

    walk(payload)
    title = payload.get("title") if isinstance(payload, dict) else None
    if not isinstance(title, str):
        title = None
    return RequirementMeta(file_name=req_name, title=title, text_blob="\n".join(parts), lookup_source=lookup_source)


def classify_targets(text: str, requirement_text: str) -> tuple[bool, bool]:
    implementation_target = False
    public_target = False

    for _quote, literal in STRING_PAT.findall(text):
        low = literal.lower()
        if len(low) < 3:
            continue
        if any(segment in low for segment in PUBLIC_SEGMENTS):
            public_target = True
        if any(segment in low for segment in PUBLIC_AMBIGUOUS_SEGMENTS):
            if PUBLIC_REQUIREMENT_HINTS.search(requirement_text):
                public_target = True
            else:
                implementation_target = True
        if any(segment in low for segment in IMPLEMENTATION_SEGMENTS):
            implementation_target = True
        if re.search(r"\.(cpp|cc|c|h|hpp|java|go|py|ts|js)$", low):
            if not any(segment in low for segment in PUBLIC_AMBIGUOUS_SEGMENTS):
                implementation_target = True

    if SPECIAL_PUBLIC_HELPERS.search(text):
        public_target = True
    if SPECIAL_IMPL_HELPERS.search(text):
        implementation_target = True

    return implementation_target, public_target


def build_scan_reasons(text: str, implementation_target: bool, public_target: bool, public_requirement: bool) -> list[str]:
    reasons: list[str] = []
    if CONTENT_PAT.search(text):
        reasons.append("content_check_pattern")
    if AST_PAT.search(text):
        reasons.append("ast_or_getsource")
    if READ_PAT.search(text):
        reasons.append("reads_repo_files")
    if ASSERT_PAT.search(text):
        reasons.append("asserts_textual_content")
    if GREP_PAT.search(text):
        reasons.append("grep_style_probe")
    if implementation_target:
        reasons.append("implementation_path_target")
    if public_target:
        reasons.append("public_surface_target")
    if public_requirement:
        reasons.append("requirement_public_surface_hint")
    return reasons


def make_entry(task_dir: Path, file_path: Path, requirement: RequirementMeta, bucket: str, reasons: list[str]) -> dict[str, Any]:
    pr = pr_from_test_path(task_dir, file_path)
    rel = file_path.relative_to(task_dir).as_posix()
    entry_id = f"{task_dir.name}:{pr or 'shared'}:{rel}"
    return {
        "entry_id": entry_id,
        "task": task_dir.name,
        "task_dir": str(task_dir),
        "pr": pr,
        "file": rel,
        "initial_bucket": bucket,
        "scan_reason": reasons,
        "requirement_file": requirement.file_name,
        "requirement_title": requirement.title,
        "requirement_lookup_source": requirement.lookup_source,
        "status": "pending",
        "final_decision": None,
        "decision_reason": None,
        "reviewed_by": None,
        "reviewed_at": None,
        "edits": [],
    }


def scan_task(task_dir: Path) -> list[dict[str, Any]]:
    rename_map = load_rename_map(task_dir)
    slug_map = load_slug_map(task_dir)
    entries: list[dict[str, Any]] = []

    for file_path in sorted((task_dir / "tests").glob("**/*")):
        if not file_path.is_file() or not is_test_entry(file_path):
            continue
        text = file_path.read_text(encoding="utf-8", errors="replace")
        requirement = load_requirement_meta(task_dir, pr_from_test_path(task_dir, file_path), rename_map, slug_map)
        public_requirement = bool(PUBLIC_REQUIREMENT_HINTS.search(requirement.text_blob))

        if CONTENT_PAT.search(file_path.as_posix()) or CONTENT_PAT.search(text):
            reasons = build_scan_reasons(text, implementation_target=False, public_target=False, public_requirement=public_requirement)
            entries.append(make_entry(task_dir, file_path, requirement, "content_check_like", reasons))
            continue

        suspicious = bool(AST_PAT.search(text) or ((READ_PAT.search(text) or GREP_PAT.search(text)) and ASSERT_PAT.search(text)))
        if not suspicious:
            continue

        implementation_target, public_target = classify_targets(text, requirement.text_blob)
        if AST_PAT.search(text) or implementation_target:
            bucket = "strong_violation"
        elif public_target and public_requirement:
            bucket = "possible_exception"
        else:
            bucket = "strong_violation"

        reasons = build_scan_reasons(text, implementation_target, public_target, public_requirement)
        entries.append(make_entry(task_dir, file_path, requirement, bucket, reasons))

    return entries


def summarize_entries(entries: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(entry["status"] for entry in entries)
    counts["total"] = len(entries)
    return dict(counts)


def enrich_entries(task_dir: Path, entries: list[dict[str, Any]]) -> None:
    rename_map = load_rename_map(task_dir)
    slug_map = load_slug_map(task_dir)
    for entry in entries:
        pr = entry.get("pr")
        if pr is not None and not isinstance(pr, str):
            pr = str(pr)
        requirement = load_requirement_meta(task_dir, pr, rename_map, slug_map)
        entry["requirement_file"] = requirement.file_name
        entry["requirement_title"] = requirement.title
        entry["requirement_lookup_source"] = requirement.lookup_source


def render_worker_prompt(task_name: str, task_dir: Path, state_path: Path) -> str:
    return f"""# {task_name} Worker Prompt

Use goal mode.

## Objective

Process every pending static-assertion cleanup candidate recorded in:

- `{state_path}`

Your ownership is limited to:

- `{task_dir}/**`
- `{state_path}`

You are not alone in the codebase. Do not revert unrelated edits made by others. Do not edit any other task.

Use goal mode without setting any explicit `token_budget`.

## Required References

- `{SEG_SPEC}`
- `{DESIGN_SPEC}`
- `{state_path}`
- `{task_dir / "requirements" / ".rename_map.json"}`
- `{task_dir / "slug_diff_map.json"}`

## Required Work

For every entry in `state.json` whose `status` is `pending`:

1. Resolve the PR's requirement mapping yourself in this order:
   - `requirements/.rename_map.json`
   - `slug_diff_map.json`
   - direct fallback to `requirements/<pr>.yaml`
   Use the entry's `requirement_*` fields only as a hint, not as authority.
2. Read the actual test file listed in the entry.
3. Read the resolved requirement file when present.
4. Decide whether the test is:
   - a noncompliant static/source assertion that must be removed, or
   - a valid requirement-backed public-surface exception that should be kept.
5. Update the entry fields:
   - `status`
   - `final_decision`
   - `decision_reason`
   - `reviewed_by`
   - `reviewed_at`
   - `edits`
6. If the decision is `remove`:
   - delete the test file,
   - run `python3 scripts/seg_static_assertion_cleanup.py sync-pr --task {task_name} --pr <pr>`,
   - if the PR no longer has runnable tests, keep the DAG and test metadata updates produced by that helper.

## Decision Policy

Follow `docs/seg-strengthening/seg-strengthening-spec.md`.

Behavior checks are mandatory by default. Source-text, AST, repo-file, changelog/doc/config, and implementation-detail assertions are not acceptable unless the requirement explicitly makes that same non-runtime public surface the contract.

Do not keep a test just because it landed in `possible_exception`, or because the diff happened to edit docs/config/CI files.

Keep is allowed only when the requirement wording itself explicitly requires the asserted public surface, such as:

- docs text or examples
- module option or argument metadata
- config schema or documented defaults
- CI matrix or workflow metadata
- packaging metadata
- public entrypoint or CLI surface shape

If the requirement is ambiguous, runtime-oriented, only indirectly related, or the connection is inferable only from the diff/source, remove the test.

When keeping a test, `decision_reason` must cite:

- the resolved requirement file
- the relevant explicit wording
- why the asserted surface is a public contract instead of an implementation detail

## State Discipline

- Keep `state.json` authoritative for this task.
- Do not leave reviewed entries as `pending`.
- Use `kept` or `removed` as terminal statuses for handled entries.
- If you truly cannot resolve an entry, mark it `blocked` with a concrete reason.

## Finish Condition

You are done only when this task's `state.json` has no `pending` entries left.

Your final message must include:

- handled entry counts by terminal status
- files removed
- metadata files updated
- any blocked entries that still need controller attention
"""


def init_run(run_root: Path) -> None:
    if run_root.exists():
        print(f"run root already exists: {run_root}", file=sys.stderr)
        sys.exit(1)

    run_root.mkdir(parents=True, exist_ok=False)
    tasks_out = run_root / "tasks"
    tasks_out.mkdir()

    manifest_tasks: list[dict[str, Any]] = []
    total_counter: Counter[str] = Counter()

    for task_dir in task_dirs():
        entries = scan_task(task_dir)
        if not entries:
            continue

        task_out = tasks_out / task_dir.name
        task_out.mkdir()

        state_path = task_out / "state.json"
        prompt_path = task_out / "worker-prompt.md"

        state_payload = {
            "task": task_dir.name,
            "task_dir": str(task_dir),
            "state_version": 2,
            "design_spec": str(DESIGN_SPEC),
            "seg_spec": str(SEG_SPEC),
            "worker_status": "pending_spawn",
            "worker_agent_id": None,
            "worker_started_at": None,
            "worker_finished_at": None,
            "entries": entries,
            "summary": summarize_entries(entries),
        }
        write_json(state_path, state_payload)
        prompt_path.write_text(render_worker_prompt(task_dir.name, task_dir, state_path))

        bucket_counts = Counter(entry["initial_bucket"] for entry in entries)
        total_counter.update(bucket_counts)
        manifest_tasks.append(
            {
                "task": task_dir.name,
                "task_dir": str(task_dir),
                "state_file": str(state_path),
                "worker_prompt_file": str(prompt_path),
                "entry_count": len(entries),
                "initial_bucket_counts": dict(bucket_counts),
            }
        )

    manifest_payload = {
        "run_root": str(run_root),
        "created_at": utc_now_iso(),
        "design_spec": str(DESIGN_SPEC),
        "seg_spec": str(SEG_SPEC),
        "scan_policy": {
            "scope": "test entry files under tasks/task_*_seg*/tests/**",
            "buckets": ["strong_violation", "possible_exception", "content_check_like"],
            "notes": [
                "strong_violation is the default for source/AST/implementation-detail assertions",
                "possible_exception marks requirement-backed public-surface candidates for worker review",
                "content_check_like covers legacy content-check style tests",
            ],
        },
        "tasks": manifest_tasks,
        "totals": {
            "task_count": len(manifest_tasks),
            "entry_count": sum(task["entry_count"] for task in manifest_tasks),
            "initial_bucket_counts": dict(total_counter),
        },
    }
    write_json(run_root / "manifest.json", manifest_payload)


def load_task_state(state_path: Path) -> dict[str, Any]:
    payload = read_json(state_path)
    if not isinstance(payload, dict):
        raise ValueError(f"invalid state payload: {state_path}")
    if not isinstance(payload.get("entries"), list):
        raise ValueError(f"missing entries in {state_path}")
    return payload


def refresh_state_summary(task_dir: Path, state_payload: dict[str, Any]) -> dict[str, Any]:
    entries = state_payload["entries"]
    enrich_entries(task_dir, entries)
    state_payload["state_version"] = max(2, int(state_payload.get("state_version", 1)))
    state_payload["summary"] = summarize_entries(entries)
    return state_payload


def summarize_run(run_root: Path, write_back: bool) -> None:
    manifest_path = run_root / "manifest.json"
    manifest = read_json(manifest_path)
    task_summaries: list[dict[str, Any]] = []
    total_status = Counter()
    total_bucket = Counter()

    for task_info in manifest.get("tasks", []):
        state_path = Path(task_info["state_file"])
        task_dir = Path(task_info["task_dir"])
        state_payload = load_task_state(state_path)
        refresh_state_summary(task_dir, state_payload)
        if write_back:
            write_json(state_path, state_payload)
            prompt_path = Path(task_info["worker_prompt_file"])
            prompt_path.write_text(render_worker_prompt(task_info["task"], task_dir, state_path))

        task_info["summary"] = state_payload["summary"]
        task_info["worker_status"] = state_payload.get("worker_status")
        total_status.update(state_payload["summary"])
        total_bucket.update(entry["initial_bucket"] for entry in state_payload["entries"])
        task_summaries.append(
            {
                "task": task_info["task"],
                "worker_status": task_info.get("worker_status"),
                "summary": state_payload["summary"],
            }
        )

    manifest["totals"] = {
        "task_count": len(task_summaries),
        "entry_count": total_status.get("total", 0),
        "status_counts": dict(total_status),
        "initial_bucket_counts": dict(total_bucket),
    }
    if write_back:
        write_json(manifest_path, manifest)

    print(json.dumps({"run_root": str(run_root), "tasks": task_summaries, "totals": manifest["totals"]}, ensure_ascii=False, indent=2))


def update_worker_state(
    run_root: Path,
    task_name: str,
    worker_status: str | None,
    worker_agent_id: str | None,
    clear_worker_agent_id: bool,
    mark_started: bool,
    mark_finished: bool,
) -> None:
    state_path = run_root / "tasks" / task_name / "state.json"
    state_payload = load_task_state(state_path)
    task_dir = Path(state_payload["task_dir"])
    refresh_state_summary(task_dir, state_payload)

    if worker_status is not None:
        state_payload["worker_status"] = worker_status
    if clear_worker_agent_id:
        state_payload["worker_agent_id"] = None
    elif worker_agent_id is not None:
        state_payload["worker_agent_id"] = worker_agent_id
    if mark_started:
        state_payload["worker_started_at"] = utc_now_iso()
        state_payload["worker_finished_at"] = None
    if mark_finished:
        state_payload["worker_finished_at"] = utc_now_iso()

    write_json(state_path, state_payload)
    print(
        json.dumps(
            {
                "task": task_name,
                "state_file": str(state_path),
                "worker_status": state_payload.get("worker_status"),
                "worker_agent_id": state_payload.get("worker_agent_id"),
                "worker_started_at": state_payload.get("worker_started_at"),
                "worker_finished_at": state_payload.get("worker_finished_at"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def existing_dag_paths(task_dir: Path) -> list[Path]:
    candidates = [
        task_dir / "unit_dag.json",
        task_dir / "tests" / "unit_dag.json",
        task_dir / "tests" / "_unit_dag.json",
    ]
    return [path for path in candidates if path.is_file()]


def has_tests_cache_paths(task_dir: Path) -> list[Path]:
    candidates = [task_dir / "tests" / "_has_tests.json"]
    return [path for path in candidates if path.exists()]


def load_authoritative_dag(task_dir: Path) -> tuple[Path, dict[str, Any]]:
    dag_paths = existing_dag_paths(task_dir)
    if not dag_paths:
        raise FileNotFoundError(f"no unit_dag.json found for {task_dir}")
    for path in dag_paths:
        payload = read_json(path)
        if isinstance(payload, dict) and isinstance(payload.get("nodes"), list):
            return path, payload
    raise ValueError(f"could not load unit dag for {task_dir}")


def write_dag_mirrors(task_dir: Path, dag_payload: dict[str, Any]) -> list[str]:
    updated: list[str] = []
    for path in existing_dag_paths(task_dir):
        write_json(path, dag_payload)
        updated.append(str(path))
    return updated


def rewrite_has_tests_cache(task_dir: Path, dag_payload: dict[str, Any]) -> list[str]:
    ids = [
        str(node.get("id"))
        for node in dag_payload.get("nodes", [])
        if isinstance(node, dict) and node.get("has_tests") is True
    ]
    updated: list[str] = []
    for path in has_tests_cache_paths(task_dir):
        path.write_text(json.dumps(ids, ensure_ascii=False, indent=2) + "\n")
        updated.append(str(path))
    return updated


def pr_has_runnable_tests(task_dir: Path, pr: str) -> tuple[bool, list[str], str]:
    from long_horizon_bench.test_env.repo_profiles import infer_repo_id, select_test_entry_files
    from scripts.validate_per_pr import get_django_test_labels, get_pr_test_files

    repo_id = infer_repo_id(task_dir, fallback_repo_id=task_dir.name.removeprefix("task_"))
    files = get_pr_test_files(task_dir, pr)
    if repo_id == "django":
        return bool(get_django_test_labels(task_dir, pr, files)), files, repo_id
    return bool(select_test_entry_files(repo_id, files)), files, repo_id


def sync_pr(task_dir: Path, pr: str) -> None:
    authoritative_path, dag_payload = load_authoritative_dag(task_dir)
    has_runnable, files, repo_id = pr_has_runnable_tests(task_dir, pr)

    changed = False
    found = False
    for node in dag_payload.get("nodes", []):
        if not isinstance(node, dict):
            continue
        if str(node.get("id")) != pr:
            continue
        found = True
        if node.get("has_tests") is not has_runnable:
            node["has_tests"] = has_runnable
            changed = True
        break
    if not found:
        raise KeyError(f"PR {pr} not found in {authoritative_path}")

    updated_paths: list[str] = []
    if changed:
        updated_paths.extend(write_dag_mirrors(task_dir, dag_payload))
        updated_paths.extend(rewrite_has_tests_cache(task_dir, dag_payload))

    print(
        json.dumps(
            {
                "task": task_dir.name,
                "pr": pr,
                "repo_id": repo_id,
                "has_runnable_tests": has_runnable,
                "test_file_count": len(files),
                "updated_paths": updated_paths,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seg static assertion cleanup run helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create a new run root with manifest and task state files")
    init_parser.add_argument("--run-root", required=True, type=Path)

    summarize_parser = subparsers.add_parser("summarize", help="Refresh summaries from task state files")
    summarize_parser.add_argument("--run-root", required=True, type=Path)
    summarize_parser.add_argument("--write-back", action="store_true")

    sync_pr_parser = subparsers.add_parser("sync-pr", help="Sync has_tests metadata for one PR after test removals")
    sync_pr_parser.add_argument("--task", required=True)
    sync_pr_parser.add_argument("--pr", required=True)

    worker_parser = subparsers.add_parser("set-worker", help="Update task-local worker metadata in a run root")
    worker_parser.add_argument("--run-root", required=True, type=Path)
    worker_parser.add_argument("--task", required=True)
    worker_parser.add_argument("--worker-status")
    worker_parser.add_argument("--worker-agent-id")
    worker_parser.add_argument("--clear-worker-agent-id", action="store_true")
    worker_parser.add_argument("--mark-started", action="store_true")
    worker_parser.add_argument("--mark-finished", action="store_true")

    args = parser.parse_args()

    if args.command == "init":
        init_run(args.run_root.resolve())
        return
    if args.command == "summarize":
        summarize_run(args.run_root.resolve(), write_back=args.write_back)
        return
    if args.command == "sync-pr":
        sync_pr((TASKS_ROOT / args.task).resolve(), args.pr)
        return
    if args.command == "set-worker":
        update_worker_state(
            args.run_root.resolve(),
            args.task,
            args.worker_status,
            args.worker_agent_id,
            args.clear_worker_agent_id,
            args.mark_started,
            args.mark_finished,
        )
        return
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    main()
