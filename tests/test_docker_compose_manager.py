from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from long_horizon_bench.terminal import docker_compose_manager
from long_horizon_bench.task_images.strategy import DockerImageStrategy


class _DummyDockerClient:
    pass


class _DummyDockerModule:
    class errors:
        class DockerException(Exception):
            pass

        class NotFound(Exception):
            pass

    @staticmethod
    def from_env(environment=None):
        return _DummyDockerClient()


class _DummyContainer:
    def exec_run(self, _cmd: str) -> tuple[int, bytes]:
        return 0, b""


def _completed(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def _make_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    docker_image_strategy: DockerImageStrategy,
    client_image_name: str,
) -> tuple[
    docker_compose_manager.DockerComposeManager,
    list[list[str]],
    list[list[str]],
]:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text("services: {}\n")

    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )

    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name=client_image_name,
        docker_compose_path=compose_file,
        docker_image_strategy=docker_image_strategy,
    )

    compose_calls: list[list[str]] = []
    docker_calls: list[list[str]] = []
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda command: compose_calls.append(command) or _completed(command),
    )
    monkeypatch.setattr(
        manager,
        "_run_docker",
        lambda command: docker_calls.append(command) or _completed(command),
    )
    monkeypatch.setattr(manager, "_service_container", lambda *_args, **_kwargs: _DummyContainer())
    return manager, compose_calls, docker_calls


def test_start_pulls_remote_image_then_uses_no_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, compose_calls, docker_calls = _make_manager(
        tmp_path,
        monkeypatch,
        docker_image_strategy=DockerImageStrategy.REMOTE,
        client_image_name="exampleorg/lhb-task-compiler:git-ab12cd3",
    )

    manager.start()

    assert docker_calls == [["pull", "exampleorg/lhb-task-compiler:git-ab12cd3"]]
    assert compose_calls == [["up", "-d", "--no-build"]]


def test_start_builds_in_local_build_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, compose_calls, docker_calls = _make_manager(
        tmp_path,
        monkeypatch,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        client_image_name="lhb__task_demo__client",
    )

    manager.start()

    assert docker_calls == []
    assert compose_calls == [["build"], ["up", "-d"]]


def test_start_skips_build_and_pull_in_local_existing_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, compose_calls, docker_calls = _make_manager(
        tmp_path,
        monkeypatch,
        docker_image_strategy=DockerImageStrategy.LOCAL_EXISTING,
        client_image_name="lhb__task_demo__client",
    )

    manager.start()

    assert docker_calls == [["image", "inspect", "lhb__task_demo__client"]]
    assert compose_calls == [["up", "-d", "--no-build"]]


def test_start_wraps_remote_pull_failure_with_image_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _compose_calls, _docker_calls = _make_manager(
        tmp_path,
        monkeypatch,
        docker_image_strategy=DockerImageStrategy.REMOTE,
        client_image_name="exampleorg/lhb-task-compiler:git-ab12cd3",
    )

    def _fail_pull(command: list[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(
            1,
            ["docker", *command],
            stderr="pull denied",
        )

    monkeypatch.setattr(manager, "_run_docker", _fail_pull)

    with pytest.raises(RuntimeError, match="exampleorg/lhb-task-compiler:git-ab12cd3"):
        manager.start()


def test_start_checks_local_existing_image_before_compose_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, compose_calls, docker_calls = _make_manager(
        tmp_path,
        monkeypatch,
        docker_image_strategy=DockerImageStrategy.LOCAL_EXISTING,
        client_image_name="lhb__task_demo__client",
    )

    def _inspect_missing(command: list[str]) -> subprocess.CompletedProcess[str]:
        docker_calls.append(command)
        if command == ["image", "inspect", "lhb__task_demo__client"]:
            raise subprocess.CalledProcessError(
                1,
                ["docker", *command],
                stderr="No such image",
            )
        return _completed(command)

    monkeypatch.setattr(manager, "_run_docker", _inspect_missing)

    with pytest.raises(
        RuntimeError,
        match="local-existing.*remote.*local-build",
    ):
        manager.start()

    assert compose_calls == []
