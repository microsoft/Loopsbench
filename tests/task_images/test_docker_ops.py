from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from long_horizon_bench.task_images import docker_ops


def _completed(
    cmd: list[str],
    *,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=stderr)


def test_publish_flow_builds_pushes_inspects_and_tags_aliases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text("services: {}\n")

    def fake_run(*, args, cwd=None, env=None, capture_output=True, text=True, check=True):
        calls.append({"args": args, "cwd": cwd, "env": env})
        if args[:3] == ["docker", "image", "inspect"]:
            return _completed(
                args,
                stdout="exampleorg/lhb-task-compiler@sha256:deadbeef\n",
            )
        return _completed(args)

    monkeypatch.setattr(docker_ops, "_run", fake_run)

    digest = docker_ops.publish_task_image(
        compose_file=compose_file,
        image_ref="exampleorg/lhb-task-compiler:git-ab12cd3",
        platform="linux/amd64",
        alias_tags=["release-20260627", "latest"],
    )

    assert digest == "sha256:deadbeef"
    assert calls[0]["args"] == [
        "docker",
        "compose",
        "-f",
        str(compose_file.resolve()),
        "build",
        "client",
    ]
    assert calls[0]["env"]["LHB_TASK_DOCKER_CLIENT_IMAGE_NAME"] == (
        "exampleorg/lhb-task-compiler:git-ab12cd3"
    )
    assert calls[0]["env"]["LHB_TASK_DOCKER_CLIENT_CONTAINER_NAME"] == (
        "lhb_publish_client"
    )
    assert calls[0]["env"]["LHB_TASK_DOCKER_TESTER_CONTAINER_NAME"] == (
        "lhb_publish_tester"
    )
    assert calls[0]["env"]["LHB_TEST_DIR"] == "/tests"
    assert calls[0]["env"]["LHB_CONTAINER_LOGS_PATH"] == "/logs"
    assert calls[0]["env"]["LHB_CONTAINER_AGENT_LOGS_PATH"] == "/agent-logs"
    assert Path(calls[0]["env"]["LHB_TASK_LOGS_PATH"]).name == "logs"
    assert Path(calls[0]["env"]["LHB_TASK_AGENT_LOGS_PATH"]).name == "agent-logs"
    assert calls[1]["args"] == [
        "docker",
        "push",
        "exampleorg/lhb-task-compiler:git-ab12cd3",
    ]
    assert calls[2]["args"] == [
        "docker",
        "tag",
        "exampleorg/lhb-task-compiler:git-ab12cd3",
        "exampleorg/lhb-task-compiler:release-20260627",
    ]
    assert calls[3]["args"] == [
        "docker",
        "push",
        "exampleorg/lhb-task-compiler:release-20260627",
    ]
    assert calls[4]["args"] == [
        "docker",
        "tag",
        "exampleorg/lhb-task-compiler:git-ab12cd3",
        "exampleorg/lhb-task-compiler:latest",
    ]
    assert calls[5]["args"] == [
        "docker",
        "push",
        "exampleorg/lhb-task-compiler:latest",
    ]
    assert calls[6]["args"] == [
        "docker",
        "pull",
        "exampleorg/lhb-task-compiler:git-ab12cd3",
    ]
    assert calls[7]["args"] == [
        "docker",
        "image",
        "inspect",
        "--format",
        "{{join .RepoDigests \"\\n\"}}",
        "exampleorg/lhb-task-compiler:git-ab12cd3",
    ]


def test_prepare_local_task_image_pulls_and_retags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(*, args, cwd=None, env=None, capture_output=True, text=True, check=True):
        calls.append(args)
        return _completed(args)

    monkeypatch.setattr(docker_ops, "_run", fake_run)

    docker_ops.prepare_local_task_image(
        image_ref="exampleorg/lhb-task-compiler:git-ab12cd3",
        local_image_name="lhb__task_compiler__client",
    )

    assert calls == [
        ["docker", "pull", "exampleorg/lhb-task-compiler:git-ab12cd3"],
        [
            "docker",
            "tag",
            "exampleorg/lhb-task-compiler:git-ab12cd3",
            "lhb__task_compiler__client",
        ],
    ]


def test_pull_task_image_runs_docker_pull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(*, args, cwd=None, env=None, capture_output=True, text=True, check=True):
        calls.append(args)
        return _completed(args)

    monkeypatch.setattr(docker_ops, "_run", fake_run)

    docker_ops.pull_task_image("exampleorg/lhb-task-compiler:git-ab12cd3")

    assert calls == [
        ["docker", "pull", "exampleorg/lhb-task-compiler:git-ab12cd3"],
    ]


def test_extract_digest_requires_matching_repo_digest() -> None:
    with pytest.raises(ValueError, match="No repo digest found"):
        docker_ops.extract_repo_digest(
            "otherorg/lhb-task-compiler:git-ab12cd3",
            "exampleorg/lhb-task-compiler@sha256:deadbeef\n",
        )
