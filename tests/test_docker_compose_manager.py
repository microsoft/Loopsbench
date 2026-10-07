from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from loopsbench.task_images.strategy import DockerImageStrategy
from loopsbench.terminal import docker_compose_manager
from loopsbench.terminal.network_policy import NetworkPlan


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


def _completed_with_output(
    command: list[str],
    *,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=stderr)


def _inspect_payload(
    *,
    image_id: str,
    repo_digests: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "Id": image_id,
            "RepoDigests": repo_digests or [],
        }
    )


def _make_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    docker_image_strategy: DockerImageStrategy,
    client_image_name: str,
    inspect_stdout: str | None = None,
) -> tuple[
    docker_compose_manager.DockerComposeManager,
    list[list[str]],
    list[list[str]],
    list[tuple[str, list[str]]],
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
    call_log: list[tuple[str, list[str]]] = []

    if inspect_stdout is None:
        inspect_stdout = _inspect_payload(image_id="sha256:default-image-id")

    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda command: (
            compose_calls.append(command),
            call_log.append(("compose", command)),
            _completed(command),
        )[-1],
    )

    def _run_docker(command: list[str]) -> subprocess.CompletedProcess[str]:
        docker_calls.append(command)
        call_log.append(("docker", command))
        if command == [
            "image",
            "inspect",
            "--format",
            "{{json .}}",
            client_image_name,
        ]:
            return _completed_with_output(command, stdout=inspect_stdout)
        return _completed(command)

    monkeypatch.setattr(manager, "_run_docker", _run_docker)
    monkeypatch.setattr(
        manager, "_service_container", lambda *_args, **_kwargs: _DummyContainer()
    )
    return manager, compose_calls, docker_calls, call_log


def test_start_pulls_remote_image_then_records_resolution_and_uses_no_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_ref = "exampleorg/loopsbench-task-compiler:git-ab12cd3"
    manager, compose_calls, docker_calls, call_log = _make_manager(
        tmp_path,
        monkeypatch,
        docker_image_strategy=DockerImageStrategy.REMOTE,
        client_image_name=image_ref,
        inspect_stdout=_inspect_payload(
            image_id="sha256:remote-image-id",
            repo_digests=[
                "anotherorg/other-image@sha256:other-digest",
                "exampleorg/loopsbench-task-compiler@sha256:matching-digest",
            ],
        ),
    )

    assert manager.image_resolution == docker_compose_manager.DockerImageResolution(
        requested_client_image_ref=image_ref,
    )

    manager.start()

    assert docker_calls == [
        ["pull", image_ref],
        ["image", "inspect", "--format", "{{json .}}", image_ref],
    ]
    assert compose_calls == [["up", "-d", "--no-build"]]
    assert call_log == [
        ("docker", ["pull", image_ref]),
        ("docker", ["image", "inspect", "--format", "{{json .}}", image_ref]),
        ("compose", ["up", "-d", "--no-build"]),
    ]
    assert manager.image_resolution == docker_compose_manager.DockerImageResolution(
        requested_client_image_ref=image_ref,
        resolved_image_id="sha256:remote-image-id",
        resolved_repo_digest="exampleorg/loopsbench-task-compiler@sha256:matching-digest",
    )


@pytest.mark.parametrize("network", [None, NetworkPlan()])
def test_start_rejects_unsafe_task_compose_before_docker_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    network: NetworkPlan | None,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        "\n".join(
            [
                "services:",
                "  client:",
                "    image: loopsbench__task_demo__client",
                "    privileged: true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="loopsbench__task_demo__client",
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        network=network,
    )

    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda _command: pytest.fail("compose must not run for unsafe config"),
    )
    monkeypatch.setattr(
        manager,
        "_run_docker",
        lambda _command: pytest.fail("docker must not run for unsafe config"),
    )

    with pytest.raises(RuntimeError, match="privileged"):
        manager.start()


def test_start_builds_in_local_build_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, compose_calls, docker_calls, call_log = _make_manager(
        tmp_path,
        monkeypatch,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        client_image_name="loopsbench__task_demo__client",
    )

    manager.start()

    assert docker_calls == [
        ["image", "inspect", "--format", "{{json .}}", "loopsbench__task_demo__client"],
    ]
    assert compose_calls == [["build"], ["up", "-d"]]
    assert call_log == [
        ("compose", ["build"]),
        (
            "docker",
            [
                "image",
                "inspect",
                "--format",
                "{{json .}}",
                "loopsbench__task_demo__client",
            ],
        ),
        ("compose", ["up", "-d"]),
    ]


def test_start_records_local_existing_image_resolution_without_repo_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_ref = "loopsbench__task_demo__client"
    manager, compose_calls, docker_calls, call_log = _make_manager(
        tmp_path,
        monkeypatch,
        docker_image_strategy=DockerImageStrategy.LOCAL_EXISTING,
        client_image_name=image_ref,
        inspect_stdout=_inspect_payload(
            image_id="sha256:local-existing-id",
            repo_digests=[],
        ),
    )

    manager.start()

    assert docker_calls == [
        ["image", "inspect", image_ref],
        ["image", "inspect", "--format", "{{json .}}", image_ref],
    ]
    assert compose_calls == [["up", "-d", "--no-build"]]
    assert call_log == [
        ("docker", ["image", "inspect", image_ref]),
        ("docker", ["image", "inspect", "--format", "{{json .}}", image_ref]),
        ("compose", ["up", "-d", "--no-build"]),
    ]
    assert manager.image_resolution == docker_compose_manager.DockerImageResolution(
        requested_client_image_ref=image_ref,
        resolved_image_id="sha256:local-existing-id",
        resolved_repo_digest=None,
    )


def test_start_leaves_repo_digest_unset_when_no_digest_matches_requested_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_ref = "exampleorg/loopsbench-task-compiler:latest"
    manager, _compose_calls, _docker_calls, _call_log = _make_manager(
        tmp_path,
        monkeypatch,
        docker_image_strategy=DockerImageStrategy.REMOTE,
        client_image_name=image_ref,
        inspect_stdout=_inspect_payload(
            image_id="sha256:remote-image-id",
            repo_digests=[
                "mirrororg/loopsbench-task-compiler@sha256:mirror",
                "anotherorg/other@sha256:other",
            ],
        ),
    )

    manager.start()

    assert manager.image_resolution == docker_compose_manager.DockerImageResolution(
        requested_client_image_ref=image_ref,
        resolved_image_id="sha256:remote-image-id",
        resolved_repo_digest=None,
    )


def test_start_wraps_remote_pull_failure_with_image_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _compose_calls, _docker_calls, _call_log = _make_manager(
        tmp_path,
        monkeypatch,
        docker_image_strategy=DockerImageStrategy.REMOTE,
        client_image_name="exampleorg/loopsbench-task-compiler:git-ab12cd3",
    )

    def _fail_pull(command: list[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(
            1,
            ["docker", *command],
            stderr="pull denied",
        )

    monkeypatch.setattr(manager, "_run_docker", _fail_pull)

    with pytest.raises(
        RuntimeError, match="exampleorg/loopsbench-task-compiler:git-ab12cd3"
    ):
        manager.start()


def test_start_checks_local_existing_image_before_compose_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, compose_calls, docker_calls, _call_log = _make_manager(
        tmp_path,
        monkeypatch,
        docker_image_strategy=DockerImageStrategy.LOCAL_EXISTING,
        client_image_name="loopsbench__task_demo__client",
    )

    def _inspect_missing(command: list[str]) -> subprocess.CompletedProcess[str]:
        docker_calls.append(command)
        if command == ["image", "inspect", "loopsbench__task_demo__client"]:
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


def test_start_warns_and_continues_when_image_resolution_capture_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    image_ref = "exampleorg/loopsbench-task-compiler:git-ab12cd3"
    manager, compose_calls, docker_calls, call_log = _make_manager(
        tmp_path,
        monkeypatch,
        docker_image_strategy=DockerImageStrategy.REMOTE,
        client_image_name=image_ref,
    )

    def _run_docker(command: list[str]) -> subprocess.CompletedProcess[str]:
        docker_calls.append(command)
        call_log.append(("docker", command))
        if command == ["pull", image_ref]:
            return _completed(command)
        if command == ["image", "inspect", "--format", "{{json .}}", image_ref]:
            raise subprocess.CalledProcessError(
                1,
                ["docker", *command],
                stderr="inspect failed",
            )
        return _completed(command)

    monkeypatch.setattr(manager, "_run_docker", _run_docker)

    with caplog.at_level("WARNING"):
        manager.start()

    assert compose_calls == [["up", "-d", "--no-build"]]
    assert call_log == [
        ("docker", ["pull", image_ref]),
        ("docker", ["image", "inspect", "--format", "{{json .}}", image_ref]),
        ("compose", ["up", "-d", "--no-build"]),
    ]
    assert manager.image_resolution == docker_compose_manager.DockerImageResolution(
        requested_client_image_ref=image_ref,
        resolved_image_id=None,
        resolved_repo_digest=None,
    )
    assert "Failed to capture docker image resolution" in caplog.text


def test_start_raises_when_tester_container_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, compose_calls, docker_calls, call_log = _make_manager(
        tmp_path,
        monkeypatch,
        docker_image_strategy=DockerImageStrategy.REMOTE,
        client_image_name="exampleorg/loopsbench-task-compiler:git-ab12cd3",
    )
    manager._tester_container_name = "task-tester"

    service_calls: list[str] = []

    def _service_container(service: str, *_args, **_kwargs):
        service_calls.append(service)
        if service == "client":
            return _DummyContainer()
        return None

    monkeypatch.setattr(manager, "_service_container", _service_container)

    with pytest.raises(RuntimeError, match="Tester container did not start"):
        manager.start()

    assert docker_calls == [
        ["pull", "exampleorg/loopsbench-task-compiler:git-ab12cd3"],
        [
            "image",
            "inspect",
            "--format",
            "{{json .}}",
            "exampleorg/loopsbench-task-compiler:git-ab12cd3",
        ],
    ]
    assert compose_calls == [["up", "-d", "--no-build"]]
    assert call_log == [
        ("docker", ["pull", "exampleorg/loopsbench-task-compiler:git-ab12cd3"]),
        (
            "docker",
            [
                "image",
                "inspect",
                "--format",
                "{{json .}}",
                "exampleorg/loopsbench-task-compiler:git-ab12cd3",
            ],
        ),
        ("compose", ["up", "-d", "--no-build"]),
    ]
    assert service_calls == ["client", "tester"]


def test_stop_with_cleanup_runs_image_and_volume_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, compose_calls, _docker_calls, _call_log = _make_manager(
        tmp_path,
        monkeypatch,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        client_image_name="loopsbench__task_demo__client",
    )
    manager._cleanup = True

    manager.stop()

    assert compose_calls == [
        ["down"],
        ["down", "--rmi", "all", "--volumes"],
    ]


def test_compose_env_exports_loopsbench_names_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _compose_calls, _docker_calls, _call_log = _make_manager(
        tmp_path,
        monkeypatch,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        client_image_name="loopsbench__task_demo__client",
    )

    assert manager.env["LOOPSBENCH_TASK_DOCKER_CLIENT_IMAGE_NAME"] == (
        "loopsbench__task_demo__client"
    )
    assert manager.env["LOOPSBENCH_TEST_DIR"] == "/tests"
