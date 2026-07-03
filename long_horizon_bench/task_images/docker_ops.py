from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile


def _run(
    *,
    args: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    capture_output: bool = True,
    text: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=capture_output,
        text=text,
        check=check,
    )


def extract_repo_digest(image_ref: str, inspect_output: str) -> str:
    image_repo = image_ref.rsplit(":", 1)[0]
    prefix = f"{image_repo}@"
    for line in inspect_output.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            return line.split("@", 1)[1]
    raise ValueError(f"No repo digest found for {image_ref}")


def _publish_compose_env(
    *,
    image_ref: str,
    compose_file: Path,
    scratch_root: Path,
    platform: str,
) -> dict[str, str]:
    logs_path = scratch_root / "logs"
    agent_logs_path = scratch_root / "agent-logs"
    logs_path.mkdir(parents=True, exist_ok=True)
    agent_logs_path.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(
        {
            "LHB_TASK_DOCKER_CLIENT_IMAGE_NAME": image_ref,
            "LHB_TASK_DOCKER_CLIENT_CONTAINER_NAME": "lhb_publish_client",
            "LHB_TASK_DOCKER_TESTER_CONTAINER_NAME": "lhb_publish_tester",
            "LHB_TASK_DOCKER_TESTER_IMAGE_NAME": image_ref,
            "LHB_TEST_DIR": "/tests",
            "LHB_CONTAINER_LOGS_PATH": "/logs",
            "LHB_CONTAINER_AGENT_LOGS_PATH": "/agent-logs",
            "LHB_TASK_LOGS_PATH": str(logs_path),
            "LHB_TASK_AGENT_LOGS_PATH": str(agent_logs_path),
            "LHB_TASK_DIR": str(compose_file.parent.resolve()),
            "DOCKER_DEFAULT_PLATFORM": platform,
        }
    )
    return env


def publish_task_image(
    *,
    compose_file: Path,
    image_ref: str,
    platform: str,
    alias_tags: list[str] | None = None,
) -> str:
    with tempfile.TemporaryDirectory(prefix="lhb-task-image-publish-") as tmpdir:
        env = _publish_compose_env(
            image_ref=image_ref,
            compose_file=compose_file,
            scratch_root=Path(tmpdir),
            platform=platform,
        )
        _run(
            args=[
                "docker",
                "compose",
                "-f",
                str(compose_file.resolve()),
                "build",
                "client",
            ],
            cwd=compose_file.parent,
            env=env,
        )
    _run(args=["docker", "push", image_ref])

    for alias_tag in alias_tags or []:
        alias_ref = f"{image_ref.rsplit(':', 1)[0]}:{alias_tag}"
        _run(args=["docker", "tag", image_ref, alias_ref])
        _run(args=["docker", "push", alias_ref])

    _run(args=["docker", "pull", image_ref])
    inspected = _run(
        args=[
            "docker",
            "image",
            "inspect",
            "--format",
            "{{join .RepoDigests \"\\n\"}}",
            image_ref,
        ]
    )
    return extract_repo_digest(image_ref, inspected.stdout)


def pull_task_image(image_ref: str) -> None:
    _run(args=["docker", "pull", image_ref])


def prepare_local_task_image(
    *,
    image_ref: str,
    local_image_name: str,
) -> None:
    _run(args=["docker", "pull", image_ref])
    _run(args=["docker", "tag", image_ref, local_image_name])
