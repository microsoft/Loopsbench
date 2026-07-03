from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from long_horizon_bench.task_images.registry import (
    ManifestImageRecord,
    build_manifest_records,
    discover_task_images,
    discover_nonseg_task_images,
    load_manifest,
    local_client_image_name,
    normalize_task_id,
    remote_client_image_ref,
    task_image_repo,
    write_manifest,
)


def _write_task(
    task_dir: Path,
    *,
    compose_body: str,
    compose_file: str = "docker-compose.yaml",
) -> None:
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        textwrap.dedent(
            f"""
            instruction: demo
            author_name: test
            author_email: test@example.com
            difficulty: hard
            category: systems
            parser_name: pytest
            docker:
              compose_file: {compose_file}
            """
        ).strip()
        + "\n"
    )
    (task_dir / "solution.sh").write_text("#!/bin/sh\n")
    (task_dir / "run-tests.sh").write_text("#!/bin/sh\n")
    (task_dir / "tests").mkdir()
    (task_dir / "tests" / "test_demo.py").write_text(
        "def test_demo():\n    assert True\n"
    )
    compose_path = task_dir / compose_file
    compose_path.parent.mkdir(parents=True, exist_ok=True)
    compose_path.write_text(textwrap.dedent(compose_body).strip() + "\n")
    (task_dir / "Dockerfile").write_text("FROM python:3.13-slim\n")


def test_normalize_repo_and_local_name() -> None:
    task_id = "task_ml_four_assignments"
    assert normalize_task_id(task_id) == "task-ml-four-assignments"
    assert task_image_repo("exampleorg", task_id) == (
        "exampleorg/lhb-task-ml-four-assignments"
    )
    assert local_client_image_name(task_id) == (
        "lhb__task_ml_four_assignments__client"
    )


def test_remote_image_ref_normalizes_seg_task() -> None:
    assert task_image_repo("exampleorg", "task_hadoop_seg05") == (
        "exampleorg/lhb-task-hadoop-seg05"
    )
    assert remote_client_image_ref(
        "exampleorg",
        "task_hadoop_seg05",
        "git-ab12cd3",
    ) == "exampleorg/lhb-task-hadoop-seg05:git-ab12cd3"


def test_discover_task_images_includes_seg_tasks(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    _write_task(
        tasks_root / "task_compiler",
        compose_body="""
        services:
          client:
            build:
              context: .
              dockerfile: Dockerfile
            image: ${LHB_TASK_DOCKER_CLIENT_IMAGE_NAME}
        """,
    )
    _write_task(
        tasks_root / "task_hadoop_seg05",
        compose_body="""
        services:
          client:
            build: .
            image: ${LHB_TASK_DOCKER_CLIENT_IMAGE_NAME}
        """,
    )

    discovered = discover_task_images(tasks_root)

    assert [item.task_id for item in discovered] == [
        "task_compiler",
        "task_hadoop_seg05",
    ]


def test_discover_nonseg_tasks_and_build_manifest(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    _write_task(
        tasks_root / "task_compiler",
        compose_body="""
        services:
          client:
            build:
              context: .
              dockerfile: Dockerfile
            image: ${LHB_TASK_DOCKER_CLIENT_IMAGE_NAME}
        """,
    )
    _write_task(
        tasks_root / "task_graph_processing",
        compose_body="""
        services:
          client:
            build: .
            image: ${LHB_TASK_DOCKER_CLIENT_IMAGE_NAME}
        """,
    )
    _write_task(
        tasks_root / "task_django_seg01",
        compose_body="""
        services:
          client:
            build: .
            image: ${LHB_TASK_DOCKER_CLIENT_IMAGE_NAME}
        """,
    )
    _write_task(
        tasks_root / "task_nested_compose",
        compose_file="compose/docker-compose.yaml",
        compose_body="""
        services:
          client:
            build:
              context: ..
              dockerfile: Dockerfile
            image: ${LHB_TASK_DOCKER_CLIENT_IMAGE_NAME}
        """,
    )

    discovered = discover_nonseg_task_images(tasks_root)

    assert [item.task_id for item in discovered] == [
        "task_compiler",
        "task_graph_processing",
        "task_nested_compose",
    ]
    assert discovered[0].dockerfile_path.as_posix().endswith(
        "task_compiler/Dockerfile"
    )
    assert discovered[1].dockerfile_path.as_posix().endswith(
        "task_graph_processing/Dockerfile"
    )
    assert discovered[2].compose_file.as_posix().endswith(
        "task_nested_compose/compose/docker-compose.yaml"
    )
    assert discovered[2].dockerfile_path.as_posix().endswith(
        "task_nested_compose/Dockerfile"
    )

    records = build_manifest_records(
        tasks_root=tasks_root,
        namespace="exampleorg",
        tag="git-ab12cd3",
        platform="linux/amd64",
        source_ref="git-ab12cd3",
    )

    assert [record.task_id for record in records] == [
        "task_compiler",
        "task_graph_processing",
        "task_nested_compose",
    ]
    assert records[0].image_repo == "exampleorg/lhb-task-compiler"
    assert records[0].image_ref == "exampleorg/lhb-task-compiler:git-ab12cd3"
    assert records[0].verified is False
    assert records[0].compose_file == "tasks/task_compiler/docker-compose.yaml"
    assert records[2].compose_file == "tasks/task_nested_compose/compose/docker-compose.yaml"


def test_manifest_round_trip(tmp_path: Path) -> None:
    manifest_path = tmp_path / "nonseg.yaml"
    records = [
        ManifestImageRecord(
            task_id="task_compiler",
            image_repo="exampleorg/lhb-task-compiler",
            image_tag="git-ab12cd3",
            image_ref="exampleorg/lhb-task-compiler:git-ab12cd3",
            image_digest="sha256:abc123",
            platform="linux/amd64",
            compose_file="tasks/task_compiler/docker-compose.yaml",
            dockerfile_path="tasks/task_compiler/Dockerfile",
            source_ref="git-ab12cd3",
            published_at="2026-06-27T00:00:00Z",
            verified=True,
            notes="verified in smoke test",
        )
    ]

    write_manifest(manifest_path, records)
    loaded = load_manifest(manifest_path)

    assert yaml.safe_load(manifest_path.read_text())["images"] == [
        item.model_dump(mode="json") for item in loaded
    ]
    assert loaded == records
