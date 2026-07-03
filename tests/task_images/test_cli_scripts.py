from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from long_horizon_bench.task_images.registry import ManifestImageRecord, write_manifest


def _load_script(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_task(task_dir: Path) -> None:
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        textwrap.dedent(
            """
            instruction: demo
            author_name: test
            author_email: test@example.com
            difficulty: hard
            category: systems
            parser_name: pytest
            docker:
              compose_file: docker-compose.yaml
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
    (task_dir / "docker-compose.yaml").write_text(
        textwrap.dedent(
            """
            services:
              client:
                build:
                  context: .
                  dockerfile: Dockerfile
                image: ${LHB_TASK_DOCKER_CLIENT_IMAGE_NAME}
            """
        ).strip()
        + "\n"
    )
    (task_dir / "Dockerfile").write_text("FROM python:3.13-slim\n")


@pytest.mark.parametrize(
    "script_name",
    [
        "generate_nonseg_image_manifest.py",
        "publish_nonseg_task_images.py",
        "prepare_nonseg_task_image.py",
        "publish_task_images.py",
    ],
)
def test_scripts_support_direct_help_invocation(script_name: str) -> None:
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / script_name), "--help"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "usage:" in completed.stdout.lower()


def test_generate_manifest_script_writes_manifest(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    _write_task(tasks_root / "task_compiler")
    _write_task(tasks_root / "task_django_seg01")
    manifest_path = tmp_path / "metadata" / "docker_images" / "nonseg.yaml"

    module = _load_script(REPO_ROOT / "scripts" / "generate_nonseg_image_manifest.py")
    exit_code = module.main(
        [
            "--tasks-root",
            str(tasks_root),
            "--manifest",
            str(manifest_path),
            "--namespace",
            "exampleorg",
            "--tag",
            "git-ab12cd3",
            "--platform",
            "linux/amd64",
            "--source-ref",
            "git-ab12cd3",
        ]
    )

    assert exit_code == 0
    manifest = yaml.safe_load(manifest_path.read_text())
    assert [item["task_id"] for item in manifest["images"]] == ["task_compiler"]
    assert manifest["images"][0]["image_ref"] == (
        "exampleorg/lhb-task-compiler:git-ab12cd3"
    )


def test_publish_script_updates_manifest(monkeypatch, tmp_path: Path) -> None:
    manifest_path = tmp_path / "metadata" / "docker_images" / "nonseg.yaml"
    manifest_path.parent.mkdir(parents=True)
    write_manifest(
        manifest_path,
        [
            ManifestImageRecord(
                task_id="task_compiler",
                image_repo="exampleorg/lhb-task-compiler",
                image_tag="git-ab12cd3",
                image_ref="exampleorg/lhb-task-compiler:git-ab12cd3",
                image_digest=None,
                platform="linux/amd64",
                compose_file="tasks/task_compiler/docker-compose.yaml",
                dockerfile_path="tasks/task_compiler/Dockerfile",
                source_ref="git-ab12cd3",
                published_at=None,
                verified=False,
                notes="",
            )
        ],
    )
    tasks_root = tmp_path / "tasks"
    _write_task(tasks_root / "task_compiler")

    module = _load_script(REPO_ROOT / "scripts" / "publish_nonseg_task_images.py")

    publish_calls: list[dict[str, object]] = []

    def fake_publish_task_image(**kwargs):
        publish_calls.append(kwargs)
        return "sha256:deadbeef"

    monkeypatch.setattr(module, "publish_task_image", fake_publish_task_image)

    exit_code = module.main(
        [
            "--manifest",
            str(manifest_path),
            "--tasks-root",
            str(tasks_root),
            "--task-id",
            "task_compiler",
            "--alias-tag",
            "release-20260627",
            "--notes",
            "published by test",
        ]
    )

    assert exit_code == 0
    assert publish_calls[0]["alias_tags"] == ["release-20260627"]
    updated = yaml.safe_load(manifest_path.read_text())["images"][0]
    assert updated["image_digest"] == "sha256:deadbeef"
    assert updated["verified"] is True
    assert updated["notes"] == "published by test"
    assert updated["published_at"].endswith("Z")


def test_prepare_script_uses_manifest(monkeypatch, tmp_path: Path) -> None:
    manifest_path = tmp_path / "metadata" / "docker_images" / "nonseg.yaml"
    manifest_path.parent.mkdir(parents=True)
    write_manifest(
        manifest_path,
        [
            ManifestImageRecord(
                task_id="task_compiler",
                image_repo="exampleorg/lhb-task-compiler",
                image_tag="git-ab12cd3",
                image_ref="exampleorg/lhb-task-compiler:git-ab12cd3",
                image_digest="sha256:deadbeef",
                platform="linux/amd64",
                compose_file="tasks/task_compiler/docker-compose.yaml",
                dockerfile_path="tasks/task_compiler/Dockerfile",
                source_ref="git-ab12cd3",
                published_at="2026-06-27T00:00:00Z",
                verified=True,
                notes="",
            )
        ],
    )

    module = _load_script(REPO_ROOT / "scripts" / "prepare_nonseg_task_image.py")
    calls: list[tuple[str, str]] = []

    def fake_prepare_local_task_image(*, image_ref: str, local_image_name: str) -> None:
        calls.append((image_ref, local_image_name))

    monkeypatch.setattr(module, "prepare_local_task_image", fake_prepare_local_task_image)

    exit_code = module.main(
        [
            "--manifest",
            str(manifest_path),
            "--task-id",
            "task_compiler",
        ]
    )

    assert exit_code == 0
    assert calls == [
        (
            "exampleorg/lhb-task-compiler:git-ab12cd3",
            "lhb__task_compiler__client",
        )
    ]


def test_publish_task_images_script_includes_seg_tasks(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tasks_root = tmp_path / "tasks"
    _write_task(tasks_root / "task_compiler")
    _write_task(tasks_root / "task_hadoop_seg05")

    module = _load_script(REPO_ROOT / "scripts" / "publish_task_images.py")
    calls = []

    def fake_publish_task_image(**kwargs):
        calls.append(kwargs)
        return "sha256:deadbeef"

    monkeypatch.setattr(module, "publish_task_image", fake_publish_task_image)

    exit_code = module.main(
        [
            "--tasks-root",
            str(tasks_root),
            "--namespace",
            "exampleorg",
            "--tag",
            "git-ab12cd3",
            "--platform",
            "linux/amd64",
        ]
    )

    assert exit_code == 0
    assert [call["image_ref"] for call in calls] == [
        "exampleorg/lhb-task-compiler:git-ab12cd3",
        "exampleorg/lhb-task-hadoop-seg05:git-ab12cd3",
    ]
