from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("script_name", "repo_env", "task_env", "repo_name", "task_name"),
    [
        (
            "regenerate_django_patches.py",
            "LHB_DJANGO_REPO_DIR",
            "LHB_DJANGO_TASK_DIR",
            "django",
            "task_django_seg11",
        ),
        (
            "regenerate_framework_patches.py",
            "LHB_FRAMEWORK_REPO_DIR",
            "LHB_FRAMEWORK_TASK_DIR",
            "framework",
            "task_framework_seg04",
        ),
        (
            "regenerate_typescript_patches.py",
            "LHB_TYPESCRIPT_REPO_DIR",
            "LHB_TYPESCRIPT_TASK_DIR",
            "TypeScript",
            "task_TypeScript_seg01",
        ),
    ],
)
def test_patch_regeneration_scripts_default_to_repo_relative_layout(
    monkeypatch,
    script_name: str,
    repo_env: str,
    task_env: str,
    repo_name: str,
    task_name: str,
):
    monkeypatch.delenv(repo_env, raising=False)
    monkeypatch.delenv(task_env, raising=False)

    module = _load_module(
        SCRIPTS_DIR / script_name,
        f"{Path(script_name).stem}_default_paths",
    )

    assert module.REPO_DIR == ROOT.parent / "repos" / repo_name
    assert module.TASK_DIR == ROOT / "tasks" / task_name
    assert module.BASE_DIR == module.TASK_DIR / "base"


def test_build_dataset_orchestrator_paths_allow_env_overrides(
    monkeypatch,
    tmp_path: Path,
):
    dag_dir = tmp_path / "paper_dag"
    output_dir = tmp_path / "tasks"
    monkeypatch.setenv("LHB_PAPER_DAG_DIR", str(dag_dir))
    monkeypatch.setenv("LHB_TASK_OUTPUT_DIR", str(output_dir))

    module = _load_module(
        SCRIPTS_DIR / "build_dataset_orchestrator.py",
        "build_dataset_orchestrator_env_paths",
    )

    assert Path(module.DEFAULT_DAG_DIR) == dag_dir
    assert Path(module.DEFAULT_OUTPUT_DIR) == output_dir
