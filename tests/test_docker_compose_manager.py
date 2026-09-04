from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from loopsbench.task_images.strategy import DockerImageStrategy  # noqa: E402
from loopsbench.terminal import docker_compose_manager  # noqa: E402


class _DummyDockerClient:
    def __init__(self) -> None:
        self.containers = _ContainerCollection()
        self.networks = self.containers.networks


class _DummyDockerModule:
    class errors:
        class DockerException(Exception):
            pass

        class NotFound(Exception):
            pass

    @staticmethod
    def from_env(environment=None):
        return _DummyDockerClient()


class _ContainerCollection:
    def __init__(self) -> None:
        self.containers: dict[str, _LifecycleContainer] = {}
        self.get_calls: list[str] = []
        self.networks = _NetworkCollection()

    def get(self, name: str) -> _LifecycleContainer:
        self.get_calls.append(name)
        try:
            return self.containers[name]
        except KeyError as exc:
            raise _DummyDockerModule.errors.NotFound(name) from exc


class _Network:
    def __init__(
        self,
        *,
        network_id: str = "network-id",
        compose_project: str = "task-client",
        collection: _NetworkCollection | None = None,
        name: str = "task-client_default",
    ) -> None:
        self._collection = collection
        self.name = name
        self.attrs = {
            "Id": network_id,
            "Labels": {
                "com.docker.compose.project": compose_project,
                "com.docker.compose.network": "default",
            },
        }

    def reload(self) -> None:
        pass

    def remove(self) -> None:
        if self._collection is None:
            return
        for identifier, network in list(self._collection.networks.items()):
            if network is self:
                self._collection.networks.pop(identifier, None)


class _NetworkCollection:
    def __init__(self) -> None:
        network = _Network(collection=self)
        self.networks: dict[str, _Network] = {"task-client_default": network}

    def get(self, name: str) -> _Network:
        try:
            return self.networks[name]
        except KeyError as exc:
            for network in self.networks.values():
                if network.attrs.get("Id") == name:
                    return network
            raise _DummyDockerModule.errors.NotFound(name) from exc


class _DummyContainerClient:
    def __init__(self) -> None:
        self.networks = _NetworkCollection()


class _LifecycleContainer:
    def __init__(
        self,
        collection: _ContainerCollection,
        name: str,
        *,
        container_id: str | None = None,
        attrs: dict[str, object] | None = None,
        stop_error: Exception | None = None,
        kill_error: Exception | None = None,
        remove_error: Exception | None = None,
    ) -> None:
        self._collection = collection
        self._name = name
        self.id = container_id or name
        self._stop_error = stop_error
        self._kill_error = kill_error
        self._remove_error = remove_error
        self.calls: list[tuple[str, object]] = []
        self.attrs = {**(attrs or {}), "Id": self.id, "Name": f"/{name}"}
        self.client = _DummyDockerClient()
        self.client.containers = collection
        self.client.networks = collection.networks
        collection.containers[name] = self
        collection.containers[self.id] = self

    def reload(self) -> None:
        pass

    def stop(self, *, timeout: int) -> None:
        self.calls.append(("stop", timeout))
        if self._stop_error is not None:
            raise self._stop_error

    def kill(self) -> None:
        self.calls.append(("kill", None))
        if self._kill_error is not None:
            raise self._kill_error

    def remove(self, *, force: bool) -> None:
        self.calls.append(("remove", force))
        if self._remove_error is not None:
            raise self._remove_error
        for identifier, container in list(self._collection.containers.items()):
            if container is self:
                self._collection.containers.pop(identifier, None)


class _DummyContainer:
    def __init__(
        self,
        attrs=None,
        expected_bind_sources=None,
        *,
        external_command_isolation: bool = False,
    ):
        self.attrs = attrs or {}
        self.id = self.attrs.get("Id")
        self.exec_calls: list[str] = []
        self._loopsbench_expected_bind_sources = (
            {
                "/logs": "/host/logs",
                "/agent-logs": "/host/agent-logs",
            }
            if expected_bind_sources is None
            else expected_bind_sources
        )
        self._loopsbench_external_command_isolation = external_command_isolation
        self.client = _DummyContainerClient()

    def reload(self) -> None:
        pass

    def exec_run(self, cmd: str) -> tuple[int, bytes]:
        self.exec_calls.append(cmd)
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


def _make_external_teardown_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_tester: bool = True,
) -> tuple[
    docker_compose_manager.DockerComposeManager,
    _ContainerCollection,
]:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        tester_container_name="task-tester" if with_tester else None,
        tester_image_name="task-image" if with_tester else None,
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        task_dir=tmp_path,
        external_command_isolation=True,
    )
    collection = _ContainerCollection()
    manager._client.containers = collection
    return manager, collection


def _admit_external_containers(
    manager: docker_compose_manager.DockerComposeManager,
    *,
    client: _LifecycleContainer | None = None,
    tester: _LifecycleContainer | None = None,
) -> None:
    if client is not None:
        manager._cleanup_client_container_id = client.id
        manager._admitted_client = docker_compose_manager.EffectiveContainerEvidence(
            container_id=client.id,
            started_at="2026-09-03T00:00:00Z",
            image_id="sha256:image",
        )
    if tester is not None:
        manager._cleanup_tester_container_id = tester.id
        manager._admitted_tester = docker_compose_manager.EffectiveContainerEvidence(
            container_id=tester.id,
            started_at="2026-09-03T00:00:00Z",
            image_id="sha256:image",
        )


def _seed_admitted_client_image(
    manager: docker_compose_manager.DockerComposeManager,
    image_id: str = "sha256:image",
) -> None:
    manager._admitted_client = docker_compose_manager.EffectiveContainerEvidence(
        container_id="client-id",
        started_at="2026-09-03T00:00:00Z",
        image_id=image_id,
    )


def _seed_external_image_resolution(
    manager: docker_compose_manager.DockerComposeManager,
    image_id: str = "sha256:image",
) -> None:
    manager._image_resolution = docker_compose_manager.DockerImageResolution(
        requested_client_image_ref=manager._client_image_name,
        resolved_image_id=image_id,
    )


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


def test_start_rejects_unsafe_task_compose_before_docker_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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


def test_start_records_exact_runner_owned_bind_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _compose_calls, _docker_calls, _call_log = _make_manager(
        tmp_path,
        monkeypatch,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        client_image_name="loopsbench__task_demo__client",
    )
    manager._logs_path = tmp_path / "logs"
    manager._agent_logs_path = tmp_path / "agent-logs"

    container = manager.start()

    assert container._loopsbench_expected_bind_sources == {
        "/logs": str((tmp_path / "logs").absolute()),
        "/agent-logs": str((tmp_path / "agent-logs").absolute()),
    }


def test_external_command_start_marks_container_and_skips_hidden_git_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        "services:\n  client:\n    image: task-image\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        external_command_isolation=True,
    )
    container = _DummyContainer(
        _effective_attrs(mounts=[]), external_command_isolation=True
    )
    monkeypatch.setattr(manager, "_validate_task_compose_security", lambda: None)
    monkeypatch.setattr(manager, "_run_compose", lambda command: _completed(command))
    monkeypatch.setattr(manager, "_capture_image_resolution", lambda: None)
    monkeypatch.setattr(manager, "_capture_external_cleanup_identities", lambda: None)
    monkeypatch.setattr(manager, "_service_container", lambda _service: container)
    _seed_external_image_resolution(manager)

    result = manager.start()

    assert result is container
    assert container._loopsbench_external_command_isolation is True
    assert container.exec_calls == []


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


def test_normal_agent_stop_still_swallows_compose_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _compose_calls, _docker_calls, _call_log = _make_manager(
        tmp_path,
        monkeypatch,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        client_image_name="loopsbench__task_demo__client",
    )
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda command: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, command)
        ),
    )

    manager.stop()


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


def test_docker_env_preserves_host_environment_except_rootful_docker_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "private")
    monkeypatch.setenv("LOOPX_STATE_FILE", "/private/controller.json")
    monkeypatch.setenv("DOCKER_BUILDKIT", "1")
    monkeypatch.setenv("DOCKER_HOST", "unix:///host/docker.sock")
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")

    env = docker_compose_manager.docker_env()

    assert env["PATH"] == "/usr/bin"
    assert env["OPENAI_API_KEY"] == "private"
    assert env["LOOPX_STATE_FILE"] == "/private/controller.json"
    assert env["DOCKER_BUILDKIT"] == "1"
    assert "DOCKER_HOST" not in env
    assert "DOCKER_CONTEXT" not in env


def test_docker_env_preserves_rootless_docker_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOOPSBENCH_ROOTFUL_DOCKER", "0")
    monkeypatch.setenv("DOCKER_HOST", "unix:///run/user/1000/docker.sock")
    monkeypatch.setenv("DOCKER_CONTEXT", "rootless")

    env = docker_compose_manager.docker_env()

    assert env["DOCKER_HOST"] == "unix:///run/user/1000/docker.sock"
    assert env["DOCKER_CONTEXT"] == "rootless"


def test_external_command_manager_filters_only_compose_subprocess_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "private")
    monkeypatch.setenv("LOOPX_STATE_FILE", "/private/controller.json")
    monkeypatch.setenv("KUBECONFIG", "/private/kubeconfig")
    monkeypatch.setenv("PGPASSWORD", "postgres-private")
    monkeypatch.setenv("MYSQL_PWD", "mysql-private")
    monkeypatch.setenv("PGPASSFILE", "/private/pgpass")
    monkeypatch.setenv("DATABASE_URL", "postgresql://runner:private@db.example/app")
    monkeypatch.setenv("APP_DATABASE_ENDPOINT", "mysql://runner:private@db.example/app")
    monkeypatch.setenv("ACME_SESSION", "unknown-private-state")
    monkeypatch.setenv("HOME", "/private/controller-home")
    monkeypatch.setenv("DOCKER_BUILDKIT", "1")
    monkeypatch.setenv("DOCKER_CONFIG", "/private/docker-config")
    monkeypatch.setenv("LOOPSBENCH_ROOTFUL_DOCKER", "0")
    monkeypatch.setenv("DOCKER_HOST", "unix:///run/user/1000/docker.sock")
    monkeypatch.setenv("DOCKER_CONTEXT", "rootless")
    monkeypatch.setenv("DOCKER_API_VERSION", "1.47")
    monkeypatch.setenv("DOCKER_CERT_PATH", "/runner-owned/docker-certs")
    monkeypatch.setenv("DOCKER_TLS", "1")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_DEFAULT_PLATFORM", "linux/arm64")
    monkeypatch.setenv("HTTPS_PROXY", "http://private-proxy.example")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/private/ssh-agent.sock")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("TMPDIR", "/runner-owned/tmp")

    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="loopsbench__task_demo__client",
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        external_command_isolation=True,
    )

    assert "OPENAI_API_KEY" not in manager.env
    assert "LOOPX_STATE_FILE" not in manager.env
    assert "KUBECONFIG" not in manager.env
    assert "PGPASSWORD" not in manager.env
    assert "MYSQL_PWD" not in manager.env
    assert "PGPASSFILE" not in manager.env
    assert "DATABASE_URL" not in manager.env
    assert "APP_DATABASE_ENDPOINT" not in manager.env
    assert "ACME_SESSION" not in manager.env
    assert "HOME" not in manager.env
    assert "DOCKER_BUILDKIT" not in manager.env
    assert "DOCKER_CONFIG" not in manager.env
    assert "HTTPS_PROXY" not in manager.env
    assert "SSH_AUTH_SOCK" not in manager.env
    assert manager.env["DOCKER_HOST"] == "unix:///run/user/1000/docker.sock"
    assert manager.env["DOCKER_CONTEXT"] == "rootless"
    assert manager.env["DOCKER_API_VERSION"] == "1.47"
    assert manager.env["DOCKER_CERT_PATH"] == "/runner-owned/docker-certs"
    assert manager.env["DOCKER_TLS"] == "1"
    assert manager.env["DOCKER_TLS_VERIFY"] == "1"
    assert manager.env["DOCKER_DEFAULT_PLATFORM"] == "linux/arm64"
    assert manager.env["PATH"] == "/usr/bin:/bin"
    assert manager.env["TMPDIR"] == "/runner-owned/tmp"
    assert manager.env["LOOPSBENCH_TASK_DOCKER_CLIENT_IMAGE_NAME"] == (
        "loopsbench__task_demo__client"
    )
    assert manager.env["LOOPSBENCH_TEST_DIR"] == "/tests"
    assert docker_compose_manager.docker_env()["OPENAI_API_KEY"] == "private"
    assert docker_compose_manager.docker_env()["PGPASSWORD"] == "postgres-private"


def test_external_command_manager_keeps_required_compose_interpolation_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    logs_path = tmp_path / "logs"
    agent_logs_path = tmp_path / "agent-logs"
    task_dir = tmp_path / "task"
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )

    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        tester_container_name="task-tester",
        tester_image_name="tester-image",
        docker_image_name_prefix="loopsbench-task",
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        logs_path=logs_path,
        agent_logs_path=agent_logs_path,
        task_dir=task_dir,
        external_command_isolation=True,
    )

    assert {
        name: value
        for name, value in manager.env.items()
        if name.startswith("LOOPSBENCH_")
    } == {
        "LOOPSBENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME": "task-client",
        "LOOPSBENCH_TASK_DOCKER_CLIENT_IMAGE_NAME": "task-image",
        "LOOPSBENCH_TASK_DOCKER_TESTER_CONTAINER_NAME": "task-tester",
        "LOOPSBENCH_TASK_DOCKER_TESTER_IMAGE_NAME": "tester-image",
        "LOOPSBENCH_TASK_DOCKER_NAME_PREFIX": "loopsbench-task",
        "LOOPSBENCH_CONTAINER_LOGS_PATH": "/logs",
        "LOOPSBENCH_CONTAINER_AGENT_LOGS_PATH": "/agent-logs",
        "LOOPSBENCH_TEST_DIR": "/tests",
        "LOOPSBENCH_TASK_LOGS_PATH": str(logs_path.absolute()),
        "LOOPSBENCH_TASK_AGENT_LOGS_PATH": str(agent_logs_path.absolute()),
        "LOOPSBENCH_TASK_DIR": str(task_dir.resolve()),
    }


def test_external_command_appends_client_only_volume_reset_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_compose = tmp_path / "docker-compose.yaml"
    regression_compose = tmp_path / "regression.compose.yaml"
    task_compose.write_text("services: {}\n", encoding="utf-8")
    regression_compose.write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )

    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        tester_container_name="task-tester",
        tester_image_name="task-image",
        docker_compose_path=[task_compose, regression_compose],
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        external_command_isolation=True,
    )

    assert manager._compose_files[:2] == [task_compose, regression_compose]
    override_path = manager._compose_files[-1]
    assert override_path.name == (
        docker_compose_manager._EXTERNAL_COMMAND_OVERRIDE_FILENAME
    )
    assert override_path.read_text(encoding="utf-8") == (
        docker_compose_manager._EXTERNAL_COMMAND_OVERRIDE
    )
    override = override_path.read_text(encoding="utf-8")
    assert override.count('command: ["sh", "-c", "sleep infinity"]') == 2
    assert override.count("disable: true") == 2
    assert override.count("ports: !reset []") == 2
    compose_cmd = manager._compose_cmd(["config", "--format", "json"])
    compose_paths = [
        Path(compose_cmd[index + 1])
        for index, value in enumerate(compose_cmd)
        if value == "-f"
    ]
    assert compose_paths == [path.resolve() for path in manager._compose_files]
    assert compose_cmd[:4] == ["docker", "compose", "--env-file", os.devnull]

    monkeypatch.setattr(manager, "_run_compose", lambda command: _completed(command))
    manager.stop()


def test_default_manager_does_not_add_external_volume_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )

    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
    )

    assert manager._compose_files == [compose_file]
    assert manager._external_command_override_dir is None
    assert manager._compose_cmd(["config"])[0:2] == ["docker", "compose"]
    assert "--env-file" not in manager._compose_cmd(["config"])


@pytest.mark.parametrize(
    "command",
    [
        ["config", "--format", "json"],
        ["build"],
        ["up", "-d"],
        ["down"],
        ["ps", "-q", "client"],
    ],
    ids=["config", "build", "up", "down", "ps"],
)
def test_external_command_disables_implicit_dotenv_for_every_compose_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
) -> None:
    manager, _collection = _make_external_teardown_manager(tmp_path, monkeypatch)

    compose_cmd = manager._compose_cmd(command)

    assert compose_cmd[:4] == ["docker", "compose", "--env-file", os.devnull]
    assert compose_cmd.count("--env-file") == 1
    assert compose_cmd[-len(command) :] == command


def test_external_command_override_cleanup_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        external_command_isolation=True,
    )
    override_path = manager._compose_files[-1]
    compose_calls: list[list[str]] = []
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda command: compose_calls.append(command) or _completed(command),
    )

    manager.stop()
    manager.stop()

    assert compose_calls == []
    assert not override_path.exists()
    assert manager._compose_files == [compose_file]


def test_external_command_stop_removes_known_containers_without_compose_down(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    client = _LifecycleContainer(collection, "task-client")
    tester = _LifecycleContainer(collection, "task-tester")
    manager._client_container = client
    manager._tester_container = tester
    _admit_external_containers(manager, client=client, tester=tester)
    override_path = manager._compose_files[-1]
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda command: pytest.fail(f"external stop must not run Compose: {command}"),
    )

    manager.stop()

    assert client.calls == [
        ("stop", 10),
        ("kill", None),
        ("remove", True),
    ]
    assert tester.calls == [
        ("stop", 10),
        ("kill", None),
        ("remove", True),
    ]
    assert collection.containers == {}
    assert not override_path.exists()


def test_external_command_stop_removes_only_the_captured_default_network_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(
        tmp_path, monkeypatch, with_tester=False
    )
    client = _LifecycleContainer(collection, "task-client", container_id="client-id")
    _admit_external_containers(manager, client=client)
    manager._cleanup_default_network_id = "network-id"
    network = manager._client.networks.get("network-id")

    manager.stop()

    assert client.calls == [("stop", 10), ("kill", None), ("remove", True)]
    assert network not in manager._client.networks.networks.values()
    assert manager._cleanup_default_network_id is None


def test_external_command_stop_rejects_replacement_default_network_without_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(
        tmp_path, monkeypatch, with_tester=False
    )
    manager._cleanup_default_network_id = "retired-network-id"
    replacement = manager._client.networks.networks["task-client_default"]

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="external_network_generation_replaced",
    ):
        manager.stop()

    assert manager._client.networks.networks["task-client_default"] is replacement


def test_external_command_override_is_retained_when_teardown_cannot_be_proven(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        external_command_isolation=True,
    )
    override_path = manager._compose_files[-1]
    collection = manager._client.containers
    client = _LifecycleContainer(
        collection,
        "task-client",
        remove_error=RuntimeError("remove unavailable"),
    )
    _admit_external_containers(manager, client=client)
    compose_file_sets: list[list[Path]] = []

    def _run_compose(command: list[str]) -> subprocess.CompletedProcess[str]:
        compose_file_sets.append(list(manager._compose_files))
        if len(compose_file_sets) == 1:
            raise subprocess.CalledProcessError(1, command)
        return _completed(command)

    monkeypatch.setattr(manager, "_run_compose", _run_compose)

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="external_container_teardown_unproven: task-client",
    ):
        manager.stop()

    assert override_path.exists()
    assert manager._compose_files[-1] == override_path

    client._remove_error = None
    manager.stop()

    assert compose_file_sets == []
    assert not override_path.exists()
    assert manager._compose_files == [compose_file]


def test_destroy_external_client_kills_removes_verifies_and_leaves_tester(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    client = _LifecycleContainer(collection, "task-client")
    tester = _LifecycleContainer(collection, "task-tester")
    manager._client_container = client
    manager._tester_container = tester
    manager._admitted_client = docker_compose_manager.EffectiveContainerEvidence(
        container_id=client.id,
        started_at="2026-09-03T00:00:00Z",
        image_id="sha256:image",
    )

    manager.destroy_external_client(expected_container_id="task-client")

    assert client.calls == [("kill", None), ("remove", True)]
    assert tester.calls == []
    assert manager._client_container is None
    assert manager._tester_container is tester
    assert collection.get_calls == [
        "task-client",
        "task-client",
        "task-client",
    ]


def test_destroy_external_client_is_idempotent_when_already_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    manager._client_container = None
    manager._admitted_client = docker_compose_manager.EffectiveContainerEvidence(
        container_id="task-client",
        started_at="2026-09-03T00:00:00Z",
        image_id="sha256:image",
    )

    manager.destroy_external_client(expected_container_id="task-client")
    manager.destroy_external_client(expected_container_id="task-client")

    assert manager._client_container is None
    assert collection.get_calls == ["task-client"] * 6


def test_destroy_external_client_raises_when_absence_cannot_be_proven(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    client = _LifecycleContainer(
        collection,
        "task-client",
        kill_error=RuntimeError("kill unavailable"),
        remove_error=RuntimeError("remove unavailable"),
    )
    manager._client_container = client
    manager._admitted_client = docker_compose_manager.EffectiveContainerEvidence(
        container_id=client.id,
        started_at="2026-09-03T00:00:00Z",
        image_id="sha256:image",
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="external_container_teardown_unproven: task-client",
    ):
        manager.destroy_external_client(expected_container_id="task-client")

    assert client.calls == [("kill", None), ("remove", True)]
    assert manager._client_container is client


def test_destroy_external_client_rejects_cached_generation_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    manager._client_container = _LifecycleContainer(collection, "replacement-id")
    manager._admitted_client = docker_compose_manager.EffectiveContainerEvidence(
        container_id="admitted-id",
        started_at="2026-09-03T00:00:00Z",
        image_id="sha256:image",
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="external_client_destroy_container_id_mismatch",
    ):
        manager.destroy_external_client(expected_container_id="admitted-id")

    assert collection.get_calls == []


def test_destroy_external_client_rejects_same_name_replacement_without_deleting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    admitted = _LifecycleContainer(
        collection, "task-client", container_id="admitted-id"
    )
    manager._client_container = admitted
    manager._admitted_client = docker_compose_manager.EffectiveContainerEvidence(
        container_id="admitted-id",
        started_at="2026-09-03T00:00:00Z",
        image_id="sha256:image",
    )

    def _replace(*, force: bool) -> None:
        assert force is True
        for identifier, container in list(collection.containers.items()):
            if container is admitted:
                collection.containers.pop(identifier, None)
        _LifecycleContainer(collection, "task-client", container_id="replacement-id")

    monkeypatch.setattr(admitted, "remove", _replace)

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="external_client_destroy_name_replaced",
    ):
        manager.destroy_external_client(expected_container_id="admitted-id")

    replacement = collection.containers["task-client"]
    assert replacement.id == "replacement-id"
    assert replacement.calls == []
    assert manager._client_container is admitted


def test_destroy_external_client_rejects_preexisting_same_name_replacement_without_lifecycle_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    admitted = _LifecycleContainer(
        collection, "admitted-client", container_id="admitted-id"
    )
    replacement = _LifecycleContainer(
        collection, "task-client", container_id="replacement-id"
    )
    manager._client_container = admitted
    _admit_external_containers(manager, client=admitted)

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="external_client_destroy_name_replaced",
    ):
        manager.destroy_external_client(expected_container_id="admitted-id")

    assert admitted.calls == [("kill", None), ("remove", True)]
    assert replacement.calls == []
    assert collection.containers["task-client"] is replacement


def test_destroy_external_client_proves_id_and_name_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    client = _LifecycleContainer(collection, "task-client", container_id="admitted-id")
    manager._client_container = client
    manager._admitted_client = docker_compose_manager.EffectiveContainerEvidence(
        container_id="admitted-id",
        started_at="2026-09-03T00:00:00Z",
        image_id="sha256:image",
    )

    manager.destroy_external_client(expected_container_id="admitted-id")

    assert collection.get_calls == [
        "admitted-id",
        "admitted-id",
        "task-client",
    ]
    assert manager._client_container is None


@pytest.mark.parametrize("operation", ["stop", "kill", "remove"])
def test_external_container_cleanup_accepts_not_found_operation_races(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    manager, collection = _make_external_teardown_manager(
        tmp_path, monkeypatch, with_tester=False
    )
    container = _LifecycleContainer(collection, "task-client")

    def _disappear(**_kwargs: object) -> None:
        collection.containers.pop("task-client", None)
        raise _DummyDockerModule.errors.NotFound("task-client")

    monkeypatch.setattr(container, operation, _disappear)

    manager._remove_external_container("task-client", stop_first=True)

    assert "task-client" not in collection.containers


def test_external_container_cleanup_accepts_error_followed_by_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(
        tmp_path, monkeypatch, with_tester=False
    )
    container = _LifecycleContainer(collection, "task-client")

    def _raced_remove(*, force: bool) -> None:
        assert force is True
        collection.containers.pop("task-client", None)
        raise RuntimeError("daemon response lost after removal")

    monkeypatch.setattr(container, "remove", _raced_remove)

    manager._remove_external_container("task-client", stop_first=True)

    assert "task-client" not in collection.containers


def test_external_container_cleanup_rejects_ambiguous_final_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(
        tmp_path, monkeypatch, with_tester=False
    )
    container = _LifecycleContainer(collection, "task-client")
    lookups = 0

    def _get(name: str) -> _LifecycleContainer:
        nonlocal lookups
        lookups += 1
        if lookups == 1:
            return container
        raise RuntimeError(f"daemon unavailable while checking {name}")

    monkeypatch.setattr(collection, "get", _get)

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="external_container_teardown_unproven: task-client",
    ):
        manager._remove_external_container("task-client", stop_first=True)


def test_external_container_cleanup_rejects_same_name_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(
        tmp_path, monkeypatch, with_tester=False
    )
    container = _LifecycleContainer(collection, "task-client")

    def _replace(*, force: bool) -> None:
        assert force is True
        _LifecycleContainer(collection, "task-client")

    monkeypatch.setattr(container, "remove", _replace)

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="external_container_teardown_unproven: task-client",
    ):
        manager._remove_external_container("task-client", stop_first=True)


def test_external_stop_rejects_preexisting_same_name_replacement_without_lifecycle_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(
        tmp_path, monkeypatch, with_tester=False
    )
    admitted = _LifecycleContainer(
        collection, "admitted-client", container_id="admitted-id"
    )
    replacement = _LifecycleContainer(
        collection, "task-client", container_id="replacement-id"
    )
    manager._client_container = admitted
    _admit_external_containers(manager, client=admitted)
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda command: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, command)
        ),
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="external_client_destroy_name_replaced",
    ):
        manager.stop()

    assert admitted.calls == [
        ("stop", 10),
        ("kill", None),
        ("remove", True),
    ]
    assert replacement.calls == []
    assert collection.containers["task-client"] is replacement


def test_external_stop_continues_after_one_container_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    client = _LifecycleContainer(
        collection,
        "task-client",
        remove_error=RuntimeError("remove unavailable"),
    )
    tester = _LifecycleContainer(collection, "task-tester")
    _admit_external_containers(manager, client=client, tester=tester)
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda command: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, command)
        ),
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="external_container_teardown_unproven: task-client",
    ):
        manager.stop()

    assert client.calls[-1] == ("remove", True)
    assert tester.calls[-1] == ("remove", True)
    assert "task-client" in collection.containers
    assert "task-tester" not in collection.containers


def test_external_stop_falls_back_to_known_container_cleanup_and_verifies_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    client = _LifecycleContainer(collection, "task-client")
    tester = _LifecycleContainer(
        collection,
        "task-tester",
        stop_error=RuntimeError("stop unavailable"),
    )
    manager._client_container = client
    manager._tester_container = tester
    _admit_external_containers(manager, client=client, tester=tester)
    override_path = manager._compose_files[-1]
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda command: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, command)
        ),
    )

    manager.stop()

    assert client.calls == [
        ("stop", 10),
        ("kill", None),
        ("remove", True),
    ]
    assert tester.calls == [
        ("stop", 10),
        ("kill", None),
        ("remove", True),
    ]
    assert manager._client_container is None
    assert manager._tester_container is None
    assert collection.containers == {}
    assert not override_path.exists()


def test_external_admission_failure_records_cleanup_ids_for_both_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    client = _LifecycleContainer(
        collection,
        "task-client",
        container_id="rejected-client-id",
        attrs=_effective_attrs(
            container_id="rejected-client-id",
            env=["PATH=/usr/bin", "SERVICE_AUTH=Bearer-private-value"],
            mounts=[],
        ),
    )
    tester = _LifecycleContainer(
        collection,
        "task-tester",
        container_id="uninspected-tester-id",
        attrs=_effective_attrs(
            container_id="uninspected-tester-id",
            mounts=_external_tester_mounts(tmp_path),
            compose_service="tester",
        ),
    )
    monkeypatch.setattr(manager, "_validate_task_compose_security", lambda: None)
    monkeypatch.setattr(manager, "_capture_image_resolution", lambda: None)
    _seed_external_image_resolution(manager)
    monkeypatch.setattr(
        manager,
        "_compose_service_ids",
        lambda: {
            "client": "rejected-client-id",
            "tester": "uninspected-tester-id",
        },
    )
    monkeypatch.setattr(
        manager,
        "_service_container",
        lambda service: {"client": client, "tester": tester}[service],
    )
    compose_calls: list[list[str]] = []

    def _run_compose(command: list[str]) -> subprocess.CompletedProcess[str]:
        compose_calls.append(command)
        if command == ["down"]:
            raise subprocess.CalledProcessError(1, command)
        return _completed(command)

    monkeypatch.setattr(manager, "_run_compose", _run_compose)

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="credential_isolation_unavailable",
    ):
        manager.start()

    assert manager._admitted_client is None
    assert manager._admitted_tester is None
    assert manager._cleanup_client_container_id == "rejected-client-id"
    assert manager._cleanup_tester_container_id == "uninspected-tester-id"

    manager.stop()

    assert compose_calls == [["build"], ["up", "-d"]]
    assert client.calls == [("stop", 10), ("kill", None), ("remove", True)]
    assert tester.calls == [("stop", 10), ("kill", None), ("remove", True)]
    assert collection.containers == {}


def test_external_partial_up_captures_label_proven_id_before_propagating_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    client = _LifecycleContainer(
        collection,
        "task-client",
        container_id="partial-client-id",
        attrs=_effective_attrs(
            container_id="partial-client-id",
            mounts=[],
        ),
    )
    monkeypatch.setattr(manager, "_validate_task_compose_security", lambda: None)
    monkeypatch.setattr(manager, "_capture_image_resolution", lambda: None)
    _seed_external_image_resolution(manager)
    monkeypatch.setattr(
        manager,
        "_compose_service_ids",
        lambda: {"client": "partial-client-id"},
    )

    def _run_compose(command: list[str]) -> subprocess.CompletedProcess[str]:
        if command == ["up", "-d"]:
            raise subprocess.CalledProcessError(1, command)
        return _completed(command)

    monkeypatch.setattr(manager, "_run_compose", _run_compose)

    with pytest.raises(subprocess.CalledProcessError):
        manager.start()

    assert manager._client_container is client
    assert manager._cleanup_client_container_id == "partial-client-id"
    assert manager._cleanup_tester_container_id is None

    manager.stop()

    assert client.calls == [("stop", 10), ("kill", None), ("remove", True)]
    assert collection.containers == {}


def test_external_rejected_generation_cleanup_never_mutates_same_name_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    client = _LifecycleContainer(
        collection,
        "task-client",
        container_id="rejected-client-id",
        attrs=_effective_attrs(
            container_id="rejected-client-id",
            env=["SERVICE_AUTH=Bearer-private-value"],
            mounts=[],
        ),
    )
    tester = _LifecycleContainer(
        collection,
        "task-tester",
        container_id="uninspected-tester-id",
        attrs=_effective_attrs(
            container_id="uninspected-tester-id",
            mounts=_external_tester_mounts(tmp_path),
            compose_service="tester",
        ),
    )
    monkeypatch.setattr(manager, "_validate_task_compose_security", lambda: None)
    monkeypatch.setattr(manager, "_capture_image_resolution", lambda: None)
    _seed_external_image_resolution(manager)
    monkeypatch.setattr(
        manager,
        "_compose_service_ids",
        lambda: {
            "client": "rejected-client-id",
            "tester": "uninspected-tester-id",
        },
    )
    monkeypatch.setattr(
        manager,
        "_service_container",
        lambda service: {"client": client, "tester": tester}[service],
    )

    def _run_compose(command: list[str]) -> subprocess.CompletedProcess[str]:
        if command == ["down"]:
            raise subprocess.CalledProcessError(1, command)
        return _completed(command)

    monkeypatch.setattr(manager, "_run_compose", _run_compose)

    with pytest.raises(docker_compose_manager.ContainerAdmissionError):
        manager.start()

    replacement = _LifecycleContainer(
        collection,
        "task-client",
        container_id="replacement-client-id",
        attrs=_effective_attrs(container_id="replacement-client-id", mounts=[]),
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="external_client_destroy_name_replaced",
    ):
        manager.stop()

    assert client.calls == [("stop", 10), ("kill", None), ("remove", True)]
    assert tester.calls == [("stop", 10), ("kill", None), ("remove", True)]
    assert replacement.calls == []
    assert collection.containers["task-client"] is replacement


def test_external_stop_is_idempotent_when_client_was_already_destroyed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    tester = _LifecycleContainer(collection, "task-tester")
    manager._client_container = None
    manager._tester_container = tester
    _admit_external_containers(manager, tester=tester)
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda command: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, command)
        ),
    )

    manager.stop()

    assert tester.calls == [
        ("stop", 10),
        ("kill", None),
        ("remove", True),
    ]
    assert collection.get_calls == [
        "task-client",
        "task-tester",
        "task-tester",
        "task-tester",
    ]


def test_external_stop_failure_propagates_and_preserves_override_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(
        tmp_path, monkeypatch, with_tester=False
    )
    _LifecycleContainer(
        collection,
        "task-client",
        remove_error=RuntimeError("remove unavailable"),
    )
    _admit_external_containers(manager, client=collection.containers["task-client"])
    override_path = manager._compose_files[-1]
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda command: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, command)
        ),
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="external_container_teardown_unproven: task-client",
    ):
        manager.stop()

    assert override_path.exists()
    assert manager._compose_files[-1] == override_path


def test_spin_up_container_does_not_swallow_external_teardown_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _collection = _make_external_teardown_manager(
        tmp_path, monkeypatch, with_tester=False
    )
    manager._external_command_isolation = True
    monkeypatch.setattr(
        docker_compose_manager, "DockerComposeManager", lambda **_kwargs: manager
    )
    monkeypatch.setattr(manager, "start", lambda: _DummyContainer())
    teardown_error = docker_compose_manager.ContainerAdmissionError(
        "external_container_teardown_unproven: task-client"
    )
    monkeypatch.setattr(
        manager,
        "stop",
        lambda: (_ for _ in ()).throw(teardown_error),
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="external_container_teardown_unproven: task-client",
    ):
        with docker_compose_manager.spin_up_container(
            client_container_name="task-client",
            client_image_name="task-image",
            docker_compose_path=tmp_path / "docker-compose.yaml",
        ):
            pass


def test_external_command_compose_preserves_rootless_daemon_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    client_environments: list[dict[str, str]] = []

    def _from_env(environment=None):
        client_environments.append(dict(environment or {}))
        return _DummyDockerClient()

    monkeypatch.setattr(_DummyDockerModule, "from_env", _from_env)
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    monkeypatch.setenv("LOOPSBENCH_ROOTFUL_DOCKER", "0")
    monkeypatch.setenv("DOCKER_HOST", "unix:///run/user/1000/docker.sock")
    monkeypatch.setenv("DOCKER_CONTEXT", "rootless")
    monkeypatch.setenv("PGPASSWORD", "private")
    command_environments: list[dict[str, str]] = []

    def _run(command, **kwargs):
        command_environments.append(dict(kwargs["env"]))
        return _completed(command)

    monkeypatch.setattr(subprocess, "run", _run)
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="loopsbench__task_demo__client",
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        external_command_isolation=True,
    )

    manager._run_docker(["version"])
    manager._run_compose(["version"])

    for environment in [*client_environments, *command_environments]:
        assert environment["DOCKER_HOST"] == "unix:///run/user/1000/docker.sock"
        assert environment["DOCKER_CONTEXT"] == "rootless"
    assert client_environments[0]["PGPASSWORD"] == "private"
    assert command_environments[0]["PGPASSWORD"] == "private"
    assert "PGPASSWORD" not in command_environments[1]


@pytest.mark.parametrize(
    "env_name",
    [
        "PGPASSWORD",
        "MYSQL_PWD",
        "PGPASSFILE",
        "REDISCLI_AUTH",
        "DATABASE_URL",
        "APP_DATABASE_DSN",
    ],
)
def test_external_command_compose_env_filters_common_database_credentials(
    env_name: str,
) -> None:
    env = docker_compose_manager._external_command_compose_env(
        {env_name: "private", "PATH": "/usr/bin"}
    )

    assert env == {"PATH": "/usr/bin"}


def test_external_command_compose_env_excludes_unknown_ambient_values() -> None:
    env = docker_compose_manager._external_command_compose_env(
        {
            "PATH": "/usr/bin",
            "TMPDIR": "/tmp/runner",
            "ACME_SESSION": "private",
            "CI_JOB_ID": "123",
            "HOME": "/private/home",
            "DOCKER_CONFIG": "/private/docker",
            "DOCKER_BUILDKIT": "1",
        }
    )

    assert env == {"PATH": "/usr/bin", "TMPDIR": "/tmp/runner"}


def test_external_command_rejects_unknown_service_env_even_when_interpolation_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        "services:\n"
        "  client:\n"
        "    image: task-image\n"
        "    environment:\n"
        "      ACME_SESSION: ${ACME_SESSION-}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    monkeypatch.setenv("ACME_SESSION", "host-private-state")
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        external_command_isolation=True,
    )

    def _run_compose(command: list[str]) -> subprocess.CompletedProcess[str]:
        assert command == ["config", "--format", "json"]
        assert "ACME_SESSION" not in manager.env
        return _completed_with_output(
            command,
            stdout=json.dumps(
                {
                    "services": {
                        "client": {
                            "image": "task-image",
                            "environment": {"ACME_SESSION": ""},
                            "volumes": [],
                        },
                        "tester": {"image": "task-image", "volumes": []},
                    }
                }
            ),
        )

    monkeypatch.setattr(manager, "_run_compose", _run_compose)

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="services.client.environment.ACME_SESSION",
    ):
        manager._validate_task_compose_security()


def test_external_command_missing_required_interpolation_fails_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        "services:\n"
        "  client:\n"
        "    image: task-image\n"
        "    labels:\n"
        "      session: ${ACME_SESSION:?required}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    monkeypatch.setenv("ACME_SESSION", "host-private-state")
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        external_command_isolation=True,
    )

    def _run_compose(command: list[str]) -> subprocess.CompletedProcess[str]:
        assert "ACME_SESSION" not in manager.env
        raise subprocess.CalledProcessError(
            1,
            command,
            stderr="required variable ACME_SESSION is missing a value: required",
        )

    monkeypatch.setattr(manager, "_run_compose", _run_compose)

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="Compose interpolation inspection unavailable",
    ):
        manager._validate_task_compose_security()


@pytest.mark.parametrize(
    ("compose_fragment", "blocked_field"),
    [
        ("    env_file: solver.env", "services.client.env_file"),
        ("    secrets: [provider_token]", "services.client.secrets"),
        ("    configs: [solver_config]", "services.client.configs"),
        (
            "    credential_spec: file://credential.json",
            "services.client.credential_spec",
        ),
        (
            "    environment:\n      - OPENAI_API_KEY",
            "services.client.environment.OPENAI_API_KEY",
        ),
        (
            "    build:\n      context: .\n      ssh: [default]",
            "services.client.build.ssh",
        ),
        (
            "    build:\n      context: .\n      args:\n        LOOPX_REGISTRY:",
            "services.client.build.args",
        ),
        (
            "    build:\n      context: .\n      args:\n        SAFE_BUILD_FLAG: enabled",
            "services.client.build.args",
        ),
    ],
)
def test_external_command_preprovisioning_rejects_task_injection_channels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compose_fragment: str,
    blocked_field: str,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        f"services:\n  client:\n    image: task-image\n{compose_fragment}\n",
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
        external_command_isolation=True,
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

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError, match=blocked_field
    ):
        manager.start()


@pytest.mark.parametrize(
    "volume",
    [
        "${LOOPSBENCH_TASK_LOGS_PATH}:/workspace",
        "${LOOPSBENCH_TASK_AGENT_LOGS_PATH}:/logs",
        "loopsbench_shared:/workspace",
        {
            "type": "bind",
            "source": "${LOOPSBENCH_TASK_LOGS_PATH}",
            "target": "/workspace",
        },
    ],
    ids=[
        "logs-placeholder-wrong-target",
        "agent-logs-placeholder-wrong-target",
        "task-volume-wrong-target",
        "long-syntax-wrong-target",
    ],
)
def test_external_command_static_mount_allowlist_binds_source_to_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    volume: object,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        "services:\n"
        "  client:\n"
        "    image: task-image\n"
        f"    volumes: {json.dumps([volume])}\n"
        "volumes:\n"
        "  loopsbench_shared: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        external_command_isolation=True,
    )
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda _command: pytest.fail("compose must not run for unsafe config"),
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match=r"services.client.volumes\[0\]",
    ):
        manager._validate_external_command_preprovisioning(compose_file)


def test_external_command_rejects_unsafe_mount_in_regression_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_compose = tmp_path / "docker-compose.yaml"
    regression_compose = tmp_path / "regression.compose.yaml"
    task_compose.write_text(
        "services:\n  client:\n    image: task-image\n", encoding="utf-8"
    )
    regression_compose.write_text(
        "services:\n"
        "  client:\n"
        "    volumes:\n"
        "      - ${LOOPSBENCH_TASK_LOGS_PATH}:/workspace\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        docker_compose_path=[task_compose, regression_compose],
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        external_command_isolation=True,
    )
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda _command: pytest.fail("compose must not run for unsafe overlay"),
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match=r"services.client.volumes\[0\]",
    ):
        manager._validate_task_compose_security()


def test_default_manager_keeps_existing_compose_injection_compatibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        "services:\n  client:\n    image: task-image\n    env_file: solver.env\n",
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
    )

    manager._validate_task_compose_security()


def test_external_command_rejects_interpolated_credential_before_provisioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        "services:\n  client:\n    image: task-image\n",
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
        external_command_isolation=True,
    )
    compose_calls: list[list[str]] = []

    def _run_compose(command: list[str]) -> subprocess.CompletedProcess[str]:
        compose_calls.append(command)
        if command == ["config", "--format", "json"]:
            return _completed_with_output(
                command,
                stdout=json.dumps(
                    {
                        "services": {
                            "client": {
                                "image": "task-image",
                                "environment": {
                                    "APP_ENDPOINT": (
                                        "postgresql://user:password@db/app"
                                    )
                                },
                            }
                        }
                    }
                ),
            )
        pytest.fail("provisioning must not run after interpolated rejection")

    monkeypatch.setattr(manager, "_run_compose", _run_compose)
    monkeypatch.setattr(
        manager,
        "_run_docker",
        lambda _command: pytest.fail("docker must not run before config admission"),
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="interpolated Compose config.*APP_ENDPOINT",
    ):
        manager.start()

    assert compose_calls == [["config", "--format", "json"]]


def test_external_command_rejects_sensitive_interpolation_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        "services:\n"
        "  client:\n"
        "    image: task-image\n"
        "    labels:\n"
        "      database-url: ${DATABASE_URL:-postgresql://localhost/app}\n",
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
        external_command_isolation=True,
    )
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda _command: pytest.fail("compose config must not read sensitive env"),
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="interpolation.DATABASE_URL",
    ):
        manager._validate_task_compose_security()


@pytest.mark.parametrize(
    ("expression", "sensitive_name"),
    [
        ("${UNKNOWN:-${DOCKER_HOST}}", "DOCKER_HOST"),
        ("${UNKNOWN:-${DOCKER_CONTEXT}}", "DOCKER_CONTEXT"),
        ("${UNKNOWN?use $GITHUB_TOKEN}", "GITHUB_TOKEN"),
        ("${UNKNOWN:-${OUTER:-${LOOPX_STATE_FILE}}}", "LOOPX_STATE_FILE"),
        ("$$${DOCKER_HOST}", "DOCKER_HOST"),
    ],
)
def test_external_command_rejects_nested_sensitive_interpolation_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expression: str,
    sensitive_name: str,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        "services:\n"
        "  client:\n"
        "    image: task-image\n"
        "    labels:\n"
        f"      route: {expression}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        external_command_isolation=True,
    )
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda _command: pytest.fail("compose must not inspect unsafe input"),
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match=rf"interpolation.{sensitive_name}",
    ):
        manager._validate_task_compose_security()


@pytest.mark.parametrize(
    "expression",
    [
        "$$DOCKER_HOST",
        "$${DOCKER_CONTEXT}",
        "${UNKNOWN:-$$DOCKER_HOST}",
    ],
)
def test_compose_interpolation_scanner_ignores_escaped_sensitive_references(
    expression: str,
) -> None:
    assert docker_compose_manager._sensitive_compose_env_references(expression) == set()


@pytest.mark.parametrize(
    "expression",
    [
        "${DOCKER_HOST",
        "${:-$DOCKER_HOST}",
        "${UNKNOWN:-${DOCKER_HOST}",
    ],
)
def test_external_command_rejects_malformed_interpolation_before_compose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expression: str,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        "services:\n"
        "  client:\n"
        "    image: task-image\n"
        "    labels:\n"
        f"      route: '{expression}'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        external_command_isolation=True,
    )
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda _command: pytest.fail("compose must not parse malformed input"),
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="task Compose interpolation inspection unavailable",
    ):
        manager._validate_task_compose_security()


def test_external_command_accepts_clean_interpolated_compose_before_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        "services:\n  client:\n    image: task-image\n",
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
        external_command_isolation=True,
    )
    calls: list[list[str]] = []

    def _run_compose(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return _completed_with_output(
            command,
            stdout=(
                json.dumps(
                    {
                        "services": {
                            "client": {
                                "image": "loopsbench__task_demo__client",
                                "command": _external_command_command(),
                                "entrypoint": [],
                                "healthcheck": {"disable": True},
                            },
                        }
                    }
                )
                if command == ["config", "--format", "json"]
                else ""
            ),
        )

    monkeypatch.setattr(manager, "_run_compose", _run_compose)
    manager._validate_task_compose_security()

    assert calls == [["config", "--format", "json"]]


def test_external_command_accepts_only_required_service_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        "services:\n"
        "  client:\n"
        "    image: task-image\n"
        "    environment:\n"
        "      TEST_DIR: ${LOOPSBENCH_TEST_DIR}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        external_command_isolation=True,
    )
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda command: _completed_with_output(
            command,
            stdout=json.dumps(
                {
                    "services": {
                        "client": {
                            "image": "task-image",
                            "command": _external_command_command(),
                            "entrypoint": [],
                            "healthcheck": {"disable": True},
                            "environment": {"TEST_DIR": "/tests"},
                            "volumes": [],
                        },
                    }
                }
            ),
        ),
    )

    manager._validate_task_compose_security()


@pytest.mark.parametrize(
    "source",
    ["task Compose", "interpolated Compose config"],
)
def test_external_command_rejects_unknown_service_environment_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    static_environment = (
        "    environment:\n      SERVICE_AUTH: Bearer-private-value\n"
        if source == "task Compose"
        else ""
    )
    compose_file.write_text(
        "services:\n  client:\n    image: task-image\n" + static_environment,
        encoding="utf-8",
    )
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        external_command_isolation=True,
    )
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda command: _completed_with_output(
            command,
            stdout=json.dumps(
                {
                    "services": {
                        "client": {
                            "image": "task-image",
                            "environment": {"SERVICE_AUTH": "Bearer-private-value"},
                            "volumes": [],
                        }
                    }
                }
            ),
        ),
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match=rf"Unsafe {source} configuration.*SERVICE_AUTH",
    ):
        manager._validate_task_compose_security()


@pytest.mark.parametrize("source", ["task Compose", "interpolated Compose config"])
def test_external_command_rejects_extra_service_before_provisioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    extra_service = (
        "  database:\n    image: postgres:latest\n" if source == "task Compose" else ""
    )
    compose_file.write_text(
        f"services:\n  client:\n    image: task-image\n{extra_service}",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        external_command_isolation=True,
    )
    compose_calls: list[list[str]] = []

    def _run_compose(command: list[str]) -> subprocess.CompletedProcess[str]:
        compose_calls.append(command)
        return _completed_with_output(
            command,
            stdout=json.dumps(
                {
                    "services": {
                        "client": {"image": "task-image", "volumes": []},
                        "database": {
                            "image": "postgres:latest",
                            "volumes": [],
                        },
                    }
                }
            ),
        )

    monkeypatch.setattr(manager, "_run_compose", _run_compose)
    monkeypatch.setattr(
        manager,
        "_run_docker",
        lambda _command: pytest.fail("provisioning must not start extra services"),
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match=rf"Unsafe {source} configuration.*services.database",
    ):
        manager.start()

    assert compose_calls == (
        [] if source == "task Compose" else [["config", "--format", "json"]]
    )


def test_external_command_accepts_template_and_regression_overlay_after_sanitizing_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_compose = REPO_ROOT / "tasks/_template/docker-compose.yaml"
    regression_compose = REPO_ROOT / "loopsbench/harness/regression.compose.yaml"
    task_dir = task_compose.parent.resolve()
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        tester_container_name="task-tester",
        tester_image_name="task-image",
        docker_compose_path=[task_compose, regression_compose],
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        task_dir=task_dir,
        external_command_isolation=True,
    )
    rendered_payload = {
        "services": {
            "client": {
                "image": "task-image",
                "command": _external_command_command(),
                "entrypoint": [],
                "healthcheck": {"disable": True},
                "volumes": [],
            },
            "tester": {
                "image": "task-image",
                "command": _external_command_command(),
                "entrypoint": [],
                "healthcheck": {"disable": True},
                "volumes": [
                    {
                        "type": "volume",
                        "source": "loopsbench_shared",
                        "target": "/shared",
                        "volume": {},
                    },
                    {
                        "type": "bind",
                        "source": str(task_dir / "tests"),
                        "target": "/tests",
                        "read_only": True,
                        "bind": {"create_host_path": True},
                    },
                    {
                        "type": "bind",
                        "source": str(task_dir / "run-tests.sh"),
                        "target": "/task-assets/run-tests.sh",
                        "read_only": True,
                        "bind": {"create_host_path": True},
                    },
                    {
                        "type": "bind",
                        "source": str(task_dir),
                        "target": "/task-assets/task",
                        "read_only": True,
                        "bind": {"create_host_path": True},
                    },
                ],
            },
        },
        "volumes": {"loopsbench_shared": {"name": "task-client_shared"}},
        "networks": {"default": {"name": "task-client_default", "ipam": {}}},
    }
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda command: _completed_with_output(
            command, stdout=json.dumps(rendered_payload)
        ),
    )

    manager._validate_task_compose_security()


@pytest.mark.parametrize(
    "tester_mounts",
    [
        [],
        [
            {
                "type": "volume",
                "source": "loopsbench_shared",
                "target": "/shared",
            },
            {
                "type": "bind",
                "source": "/wrong/tests",
                "target": "/tests",
                "read_only": True,
            },
        ],
        [
            {
                "type": "volume",
                "source": "loopsbench_shared",
                "target": "/shared",
            },
            {
                "type": "bind",
                "source": "TASK_DIR/tests",
                "target": "/tests",
                "read_only": False,
            },
        ],
    ],
    ids=["missing-all", "wrong-source", "writable-bind"],
)
def test_external_command_rejects_incomplete_or_changed_rendered_tester_mounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tester_mounts: list[dict[str, object]],
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        "services:\n"
        "  client:\n"
        "    image: task-image\n"
        "  tester:\n"
        "    image: task-image\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        tester_container_name="task-tester",
        tester_image_name="task-image",
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        task_dir=Path("TASK_DIR"),
        external_command_isolation=True,
    )
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda command: _completed_with_output(
            command,
            stdout=json.dumps(
                {
                    "services": {
                        "client": {"image": "task-image", "volumes": []},
                        "tester": {
                            "image": "task-image",
                            "volumes": tester_mounts,
                        },
                    }
                }
            ),
        ),
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="services.tester.volumes",
    ):
        manager._validate_task_compose_security()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("image", "task-controlled-tester:latest"),
        ("entrypoint", ["/task-owned-entrypoint"]),
        ("command", ["sh", "-c", "run task hook"]),
        ("healthcheck", {"test": ["CMD-SHELL", "curl localhost"]}),
        ("post_start", [{"command": "/task-assets/hook"}]),
        ("pre_stop", [{"command": "/task-assets/hook"}]),
    ],
    ids=["image", "entrypoint", "command", "healthcheck", "post-start", "pre-stop"],
)
def test_external_command_rejects_task_owned_rendered_tester_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        "services:\n"
        "  client:\n"
        "    image: task-image\n"
        "  tester:\n"
        "    image: task-image\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        tester_container_name="task-tester",
        tester_image_name="task-image",
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        task_dir=tmp_path,
        external_command_isolation=True,
    )
    tester_service: dict[str, object] = {
        "image": "task-image",
        "entrypoint": [],
        "command": _external_command_command(),
        "healthcheck": {"disable": True},
        "volumes": [
            {"type": "volume", "source": "loopsbench_shared", "target": "/shared"},
            {
                "type": "bind",
                "source": str(tmp_path / "tests"),
                "target": "/tests",
                "read_only": True,
            },
            {
                "type": "bind",
                "source": str(tmp_path / "run-tests.sh"),
                "target": "/task-assets/run-tests.sh",
                "read_only": True,
            },
            {
                "type": "bind",
                "source": str(tmp_path),
                "target": "/task-assets/task",
                "read_only": True,
            },
        ],
    }
    tester_service[field] = value
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda command: _completed_with_output(
            command,
            stdout=json.dumps(
                {
                    "services": {
                        "client": {
                            "image": "task-image",
                            "entrypoint": [],
                            "command": _external_command_command(),
                            "healthcheck": {"disable": True},
                            "volumes": [],
                        },
                        "tester": tester_service,
                    }
                }
            ),
        ),
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match=rf"services.tester.{field}",
    ):
        manager._validate_task_compose_security()


@pytest.mark.parametrize(
    "client_mount",
    [
        {"type": "bind", "source": "/host/logs", "target": "/logs"},
        {
            "type": "bind",
            "source": "/host/agent-logs",
            "target": "/agent-logs",
        },
        {
            "type": "volume",
            "source": "loopsbench_shared",
            "target": "/shared",
        },
        {
            "type": "volume",
            "source": "loopsbench_shared",
            "target": "/var/lib/loopsbench/.mirror",
        },
    ],
    ids=["logs", "agent-logs", "shared", "mirror"],
)
def test_external_command_rejects_any_rendered_client_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client_mount: dict[str, object],
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        "services:\n  client:\n    image: task-image\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        external_command_isolation=True,
    )
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda command: _completed_with_output(
            command,
            stdout=json.dumps(
                {
                    "services": {
                        "client": {"image": "task-image", "volumes": [client_mount]}
                    }
                }
            ),
        ),
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="services.client.volumes",
    ):
        manager._validate_task_compose_security()


@pytest.mark.parametrize(
    ("result", "error"),
    [
        (
            _completed_with_output(["config", "--format", "json"], stdout="not-json"),
            "interpolation inspection unavailable",
        ),
        (
            _completed_with_output(["config", "--format", "json"], stdout="[]"),
            "interpolated Compose config inspection unavailable",
        ),
    ],
)
def test_external_command_rejects_uninspectable_interpolated_compose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: subprocess.CompletedProcess[str],
    error: str,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        "services:\n  client:\n    image: task-image\n",
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
        external_command_isolation=True,
    )
    monkeypatch.setattr(manager, "_run_compose", lambda _command: result)

    with pytest.raises(docker_compose_manager.ContainerAdmissionError, match=error):
        manager._validate_task_compose_security()


def test_external_command_rejects_unsupported_volume_reset_before_provisioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        "services:\n  client:\n    image: task-image\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        external_command_isolation=True,
    )
    compose_calls: list[list[str]] = []

    def _run_compose(command: list[str]) -> subprocess.CompletedProcess[str]:
        compose_calls.append(command)
        raise subprocess.CalledProcessError(
            1,
            command,
            stderr="yaml: unmarshal errors: unknown tag !reset",
        )

    monkeypatch.setattr(manager, "_run_compose", _run_compose)
    monkeypatch.setattr(
        manager,
        "_run_docker",
        lambda _command: pytest.fail("docker must not run before config probe"),
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="interpolation inspection unavailable",
    ):
        manager.start()

    assert compose_calls == [["config", "--format", "json"]]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ipc", "host"),
        ("ipc", "service:controller"),
        ("pid", "host"),
        ("network_mode", "service:controller"),
        ("cgroup", "host"),
        ("userns_mode", "host"),
        ("uts", "host"),
    ],
)
def test_external_command_rejects_interpolated_shared_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        "services:\n  client:\n    image: task-image\n",
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
        external_command_isolation=True,
    )
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda command: _completed_with_output(
            command,
            stdout=json.dumps(
                {"services": {"client": {"image": "task-image", field: value}}}
            ),
        ),
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match=rf"interpolated Compose config.*services.client.{field}",
    ):
        manager._validate_task_compose_security()


@pytest.mark.parametrize(
    ("source", "compose_fragment", "rendered_fragment", "blocked_field"),
    [
        (
            "task Compose",
            "services:\n"
            "  client:\n"
            "    image: task-image\n"
            "networks:\n"
            "  controller_net:\n"
            "    external: true\n",
            {},
            "networks.controller_net",
        ),
        (
            "task Compose",
            "services:\n"
            "  client:\n"
            "    image: task-image\n"
            "    networks: [default, controller_net]\n",
            {},
            "services.client.networks",
        ),
        (
            "task Compose",
            "services:\n  client:\n    image: task-image\n    network_mode: bridge\n",
            {},
            "services.client.network_mode",
        ),
        (
            "interpolated Compose config",
            "services:\n  client:\n    image: task-image\n",
            {"networks": {"default": {"name": "other_default"}}},
            "networks.default",
        ),
        (
            "interpolated Compose config",
            "services:\n  client:\n    image: task-image\n",
            {
                "networks": {
                    "default": {
                        "name": "task-client_default",
                        "ipam": {"config": [{"subnet": "172.28.0.0/16"}]},
                    }
                }
            },
            "networks.default",
        ),
        (
            "interpolated Compose config",
            "services:\n  client:\n    image: task-image\n",
            {
                "networks": {
                    "default": {
                        "name": "task-client_default",
                        "driver": "bridge",
                    }
                }
            },
            "networks.default",
        ),
        (
            "interpolated Compose config",
            "services:\n  client:\n    image: task-image\n",
            {
                "networks": {
                    "default": {
                        "name": "task-client_default",
                        "external": True,
                    }
                }
            },
            "networks.default",
        ),
        (
            "interpolated Compose config",
            "services:\n  client:\n    image: task-image\n",
            {"services": {"client": {"networks": {"outside": {}}}}},
            "services.client.networks",
        ),
        (
            "interpolated Compose config",
            "services:\n  client:\n    image: task-image\n",
            {"services": {"client": {"ports": ["127.0.0.1:9000:9000"]}}},
            "services.client.ports",
        ),
    ],
    ids=[
        "static-external-network",
        "static-additional-network",
        "static-unnamed-bridge",
        "rendered-wrong-default-name",
        "rendered-nonempty-ipam",
        "rendered-explicit-driver",
        "rendered-external-default",
        "rendered-additional-network",
        "rendered-published-port",
    ],
)
def test_external_command_rejects_non_runner_network_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    compose_fragment: str,
    rendered_fragment: dict[str, object],
    blocked_field: str,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(compose_fragment, encoding="utf-8")
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        docker_compose_path=compose_file,
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        external_command_isolation=True,
    )
    payload: dict[str, object] = {
        "services": {
            "client": {
                "image": "task-image",
                "command": _external_command_command(),
                "entrypoint": [],
                "healthcheck": {"disable": True},
                "volumes": [],
            },
            "tester": {
                "image": "task-image",
                "command": _external_command_command(),
                "entrypoint": [],
                "healthcheck": {"disable": True},
                "volumes": [],
            },
        },
        "networks": {"default": {"name": "task-client_default"}},
    }
    for key, value in rendered_fragment.items():
        if key == "services":
            for service_name, service_fields in value.items():
                payload["services"][service_name].update(service_fields)
        else:
            payload[key] = value
    monkeypatch.setattr(
        manager,
        "_run_compose",
        lambda command: _completed_with_output(command, stdout=json.dumps(payload)),
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match=rf"Unsafe {source} configuration.*{blocked_field}",
    ):
        manager._validate_task_compose_security()


@pytest.mark.parametrize(
    ("field", "value", "compose_value"),
    [
        ("privileged", True, "${TASK_PRIVILEGED:-true}"),
        ("cap_add", ["SYS_ADMIN"], '["${TASK_CAPABILITY:-SYS_ADMIN}"]'),
        (
            "devices",
            ["/dev/kvm:/dev/kvm"],
            '["${TASK_DEVICE:-/dev/kvm}:/dev/kvm"]',
        ),
        (
            "device_cgroup_rules",
            ["c 1:3 rwm"],
            '["${TASK_DEVICE_RULE:-c 1:3 rwm}"]',
        ),
        (
            "security_opt",
            ["seccomp:unconfined"],
            '["${TASK_SECURITY_OPT:-seccomp:unconfined}"]',
        ),
        ("use_api_socket", True, "${TASK_API_SOCKET:-true}"),
        ("gpus", "all", "${TASK_GPUS:-all}"),
    ],
)
def test_external_command_rejects_interpolated_dangerous_container_settings_before_provisioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    compose_value: str,
) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        "services:\n  client:\n    image: task-image\n", encoding="utf-8"
    )
    overlay_file = tmp_path / "rendered-defaults.compose.yaml"
    overlay_file.write_text(
        f"services:\n  client:\n    {field}: {compose_value}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        docker_compose_manager,
        "_get_docker_module",
        lambda: _DummyDockerModule,
    )
    manager = docker_compose_manager.DockerComposeManager(
        client_container_name="task-client",
        client_image_name="task-image",
        docker_compose_path=[compose_file, overlay_file],
        docker_image_strategy=DockerImageStrategy.LOCAL_BUILD,
        external_command_isolation=True,
    )
    compose_calls: list[list[str]] = []

    def _run_compose(command: list[str]) -> subprocess.CompletedProcess[str]:
        compose_calls.append(command)
        if command == ["config", "--format", "json"]:
            return _completed_with_output(
                command,
                stdout=json.dumps(
                    {"services": {"client": {"image": "task-image", field: value}}}
                ),
            )
        pytest.fail("provisioning must not run after rendered isolation rejection")

    monkeypatch.setattr(manager, "_run_compose", _run_compose)
    monkeypatch.setattr(
        manager,
        "_run_docker",
        lambda _command: pytest.fail("docker must not run before config admission"),
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match=rf"interpolated Compose config.*services.client.{field}",
    ):
        manager.start()

    assert compose_calls == [["config", "--format", "json"]]


def _effective_attrs(
    *,
    container_id: str = "sha256:container",
    env: list[str] | None = None,
    image: str = "sha256:image",
    mounts: list[dict[str, object]] | None = None,
    compose_project: str | None = "task-client",
    compose_service: str = "client",
    cap_add: list[str] | None = None,
    host_config: dict[str, object] | None = None,
    networks: dict[str, object] | None = None,
    command: list[str] | None = None,
    entrypoint: list[str] | None = None,
    healthcheck: dict[str, object] | None = None,
) -> dict[str, object]:
    labels = (
        {
            "com.docker.compose.project": compose_project,
            "com.docker.compose.service": compose_service,
        }
        if compose_project is not None
        else {}
    )
    effective_host_config = {
        "Privileged": False,
        "PidMode": "",
        "IpcMode": "private",
        "CgroupnsMode": "private",
        "UsernsMode": "",
        "UTSMode": "",
        "NetworkMode": f"{compose_project}_default" if compose_project else "bridge",
        "CapAdd": [] if cap_add is None else cap_add,
        "Devices": [],
        "SecurityOpt": [],
    }
    effective_host_config.update(host_config or {})
    return {
        "Id": container_id,
        "Image": image,
        "State": {"Running": True, "StartedAt": "2026-09-03T00:00:00Z"},
        "Config": {
            "Env": ["PATH=/usr/bin"] if env is None else env,
            "Labels": labels,
            "Entrypoint": [] if entrypoint is None else entrypoint,
            "Cmd": _external_command_command() if command is None else command,
            "Healthcheck": ({"Test": ["NONE"]} if healthcheck is None else healthcheck),
        },
        "HostConfig": effective_host_config,
        "NetworkSettings": {
            "Networks": (
                {
                    f"{compose_project}_default": {
                        "NetworkID": "network-id",
                        "Aliases": [compose_service],
                    }
                }
                if networks is None and compose_project is not None
                else networks or {}
            )
        },
        "Mounts": (
            [
                {
                    "Type": "bind",
                    "Source": "/host/logs",
                    "Destination": "/logs",
                }
            ]
            if mounts is None
            else mounts
        ),
    }


def _external_command_command() -> list[str]:
    return ["sh", "-c", "sleep infinity"]


def _external_tester_mounts(task_dir: Path) -> list[dict[str, object]]:
    return [
        {
            "Type": "volume",
            "Name": "task-client_loopsbench_shared",
            "Source": "/var/lib/docker/volumes/task-client_loopsbench_shared/_data",
            "Destination": "/shared",
            "RW": True,
        },
        {
            "Type": "bind",
            "Source": str(task_dir / "tests"),
            "Destination": "/tests",
            "RW": False,
        },
        {
            "Type": "bind",
            "Source": str(task_dir / "run-tests.sh"),
            "Destination": "/task-assets/run-tests.sh",
            "RW": False,
        },
        {
            "Type": "bind",
            "Source": str(task_dir),
            "Destination": "/task-assets/task",
            "RW": False,
        },
    ]


def test_validate_live_client_returns_the_admitted_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    _LifecycleContainer(
        collection,
        "task-client",
        container_id="client-id",
        attrs=_effective_attrs(container_id="client-id", mounts=[]),
    )
    manager._admitted_client = docker_compose_manager.EffectiveContainerEvidence(
        container_id="client-id",
        started_at="2026-09-03T00:00:00Z",
        image_id="sha256:image",
    )

    evidence = manager.validate_live_client()

    assert evidence == manager._admitted_client
    assert collection.get_calls == ["client-id", "task-client"]


def test_validate_live_client_rejects_same_name_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    _LifecycleContainer(
        collection,
        "admitted-client",
        container_id="client-id",
        attrs=_effective_attrs(container_id="client-id", mounts=[]),
    )
    replacement = _LifecycleContainer(
        collection,
        "task-client",
        container_id="replacement-id",
        attrs=_effective_attrs(container_id="replacement-id", mounts=[]),
    )
    manager._client_container = replacement
    manager._admitted_client = docker_compose_manager.EffectiveContainerEvidence(
        container_id="client-id",
        started_at="2026-09-03T00:00:00Z",
        image_id="sha256:image",
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="live_container_generation_mismatch",
    ):
        manager.validate_live_client()

    assert replacement.calls == []


def test_validate_live_tester_accepts_only_admitted_generation_and_exact_mounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    tester = _LifecycleContainer(
        collection,
        "task-tester",
        container_id="tester-id",
        attrs=_effective_attrs(
            container_id="tester-id",
            mounts=_external_tester_mounts(tmp_path),
            compose_service="tester",
        ),
    )
    manager._tester_container = tester
    _seed_admitted_client_image(manager)
    manager._admitted_tester = docker_compose_manager.EffectiveContainerEvidence(
        container_id="tester-id",
        started_at="2026-09-03T00:00:00Z",
        image_id="sha256:image",
    )

    evidence = manager.validate_live_tester()

    assert evidence == manager._admitted_tester
    assert collection.get_calls == ["tester-id", "task-tester"]


def test_validate_live_tester_accepts_docker_desktop_host_mnt_bind_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    mounts = _external_tester_mounts(tmp_path)
    for mount in mounts:
        if mount["Type"] == "bind":
            mount["Source"] = f"/host_mnt{mount['Source']}"
    _LifecycleContainer(
        collection,
        "task-tester",
        container_id="tester-id",
        attrs=_effective_attrs(
            container_id="tester-id", mounts=mounts, compose_service="tester"
        ),
    )
    _seed_admitted_client_image(manager)
    manager._admitted_tester = docker_compose_manager.EffectiveContainerEvidence(
        container_id="tester-id",
        started_at="2026-09-03T00:00:00Z",
        image_id="sha256:image",
    )

    evidence = manager.validate_live_tester()

    assert evidence == manager._admitted_tester


def test_validate_live_tester_rejects_image_different_from_admitted_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    _LifecycleContainer(
        collection,
        "task-tester",
        container_id="tester-id",
        attrs=_effective_attrs(
            container_id="tester-id",
            image="sha256:replacement-image",
            mounts=_external_tester_mounts(tmp_path),
            compose_service="tester",
        ),
    )
    _seed_admitted_client_image(manager, "sha256:client-image")
    manager._admitted_tester = docker_compose_manager.EffectiveContainerEvidence(
        container_id="tester-id",
        started_at="2026-09-03T00:00:00Z",
        image_id="sha256:replacement-image",
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="external_tester_image_invalid",
    ):
        manager.validate_live_tester()


@pytest.mark.parametrize(
    ("entrypoint", "command", "healthcheck", "error"),
    [
        (["/task-owned-entrypoint"], None, None, "entrypoint"),
        (None, ["sh", "-c", "run task hook"], None, "entrypoint"),
        (None, None, {"Test": ["CMD-SHELL", "curl localhost"]}, "healthcheck"),
    ],
    ids=["entrypoint", "command", "healthcheck"],
)
def test_validate_live_tester_rejects_task_owned_launcher_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: list[str] | None,
    command: list[str] | None,
    healthcheck: dict[str, object] | None,
    error: str,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    attrs = _effective_attrs(
        container_id="tester-id",
        mounts=_external_tester_mounts(tmp_path),
        compose_service="tester",
    )
    if entrypoint is not None:
        attrs["Config"]["Entrypoint"] = entrypoint
    if command is not None:
        attrs["Config"]["Cmd"] = command
    if healthcheck is not None:
        attrs["Config"]["Healthcheck"] = healthcheck
    _LifecycleContainer(
        collection,
        "task-tester",
        container_id="tester-id",
        attrs=attrs,
    )
    _seed_admitted_client_image(manager)
    manager._admitted_tester = docker_compose_manager.EffectiveContainerEvidence(
        container_id="tester-id",
        started_at="2026-09-03T00:00:00Z",
        image_id="sha256:image",
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match=rf"effective_container_{error}_invalid",
    ):
        manager.validate_live_tester()


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "extra",
        "shared-read-only",
        "bind-writable",
        "host-mnt-bind-writable",
        "wrong-source",
        "wrong-volume",
        "missing-rw",
    ],
)
def test_validate_live_tester_rejects_any_mount_distinction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    mounts = _external_tester_mounts(tmp_path)
    if mutation == "missing":
        mounts.pop()
    elif mutation == "extra":
        mounts.append(
            {
                "Type": "volume",
                "Name": "anonymous",
                "Source": "/var/lib/docker/volumes/anonymous/_data",
                "Destination": "/image-data",
                "RW": True,
            }
        )
    elif mutation == "shared-read-only":
        mounts[0]["RW"] = False
    elif mutation == "bind-writable":
        mounts[1]["RW"] = True
    elif mutation == "host-mnt-bind-writable":
        mounts[1]["Source"] = f"/host_mnt{mounts[1]['Source']}"
        mounts[1]["RW"] = True
    elif mutation == "wrong-source":
        mounts[1]["Source"] = str(tmp_path / "other-tests")
    elif mutation == "wrong-volume":
        mounts[0]["Name"] = "anonymous"
    elif mutation == "missing-rw":
        mounts[2].pop("RW")

    _LifecycleContainer(
        collection,
        "task-tester",
        container_id="tester-id",
        attrs=_effective_attrs(
            container_id="tester-id", mounts=mounts, compose_service="tester"
        ),
    )
    _seed_admitted_client_image(manager)
    manager._admitted_tester = docker_compose_manager.EffectiveContainerEvidence(
        container_id="tester-id",
        started_at="2026-09-03T00:00:00Z",
        image_id="sha256:image",
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="external_tester_mount|effective_container_inspection",
    ):
        manager.validate_live_tester()


def test_validate_live_tester_rejects_same_name_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, collection = _make_external_teardown_manager(tmp_path, monkeypatch)
    admitted = _LifecycleContainer(
        collection,
        "admitted-tester",
        container_id="tester-id",
        attrs=_effective_attrs(
            container_id="tester-id",
            mounts=_external_tester_mounts(tmp_path),
            compose_service="tester",
        ),
    )
    replacement = _LifecycleContainer(
        collection,
        "task-tester",
        container_id="replacement-id",
        attrs=_effective_attrs(
            container_id="replacement-id",
            mounts=_external_tester_mounts(tmp_path),
            compose_service="tester",
        ),
    )
    manager._tester_container = replacement
    _seed_admitted_client_image(manager)
    manager._admitted_tester = docker_compose_manager.EffectiveContainerEvidence(
        container_id="tester-id",
        started_at="2026-09-03T00:00:00Z",
        image_id="sha256:image",
    )

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="live_container_generation_mismatch",
    ):
        manager.validate_live_tester()

    assert collection.get_calls == ["tester-id", "task-tester"]
    assert admitted.calls == []
    assert replacement.calls == []


def test_effective_container_inspection_returns_private_binding_evidence() -> None:
    evidence = docker_compose_manager.DockerComposeManager.inspect_effective_container(
        _DummyContainer(_effective_attrs())
    )

    assert evidence.container_id == "sha256:container"
    assert evidence.image_id == "sha256:image"


def test_effective_container_preserves_task_required_net_admin() -> None:
    evidence = docker_compose_manager.DockerComposeManager.inspect_effective_container(
        _DummyContainer(_effective_attrs(cap_add=["NET_ADMIN"]))
    )

    assert evidence.container_id == "sha256:container"


def test_external_command_effective_container_requires_zero_mounts() -> None:
    container = _DummyContainer(
        _effective_attrs(mounts=[]),
        external_command_isolation=True,
    )
    container._loopsbench_expected_compose_project = "task-client"
    container._loopsbench_expected_compose_service = "client"
    evidence = docker_compose_manager.DockerComposeManager.inspect_effective_container(
        container
    )

    assert evidence.container_id == "sha256:container"


def test_effective_container_treats_null_devices_as_empty() -> None:
    evidence = docker_compose_manager.DockerComposeManager.inspect_effective_container(
        _DummyContainer(_effective_attrs(host_config={"Devices": None}))
    )

    assert evidence.container_id == "sha256:container"


@pytest.mark.parametrize(
    "mount",
    [
        {
            "Type": "bind",
            "Source": "/host/logs",
            "Destination": "/logs",
        },
        {
            "Type": "volume",
            "Name": "task-client_loopsbench_shared",
            "Source": "/var/lib/docker/volumes/task-client_loopsbench_shared/_data",
            "Destination": "/shared",
        },
        {
            "Type": "volume",
            "Name": "task-client_loopsbench_shared",
            "Source": "/var/lib/docker/volumes/task-client_loopsbench_shared/_data",
            "Destination": "/var/lib/loopsbench/.mirror",
        },
    ],
    ids=["logs-bind", "shared-volume", "mirror-volume"],
)
def test_external_command_effective_container_rejects_every_residual_mount(
    mount: dict[str, object],
) -> None:
    container = _DummyContainer(
        _effective_attrs(mounts=[mount]),
        external_command_isolation=True,
    )
    container._loopsbench_expected_compose_project = "task-client"
    container._loopsbench_expected_compose_service = "client"
    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="effective_container_isolation_invalid",
    ):
        docker_compose_manager.DockerComposeManager.inspect_effective_container(
            container
        )


def test_external_command_effective_container_accepts_explicit_isolation_context() -> (
    None
):
    container = _DummyContainer(_effective_attrs(mounts=[]))
    container._loopsbench_expected_compose_project = "task-client"
    container._loopsbench_expected_compose_service = "client"

    evidence = docker_compose_manager.DockerComposeManager.inspect_effective_container(
        container, external_command_isolation=True
    )

    assert evidence.container_id == "sha256:container"
    container.attrs["Mounts"] = [
        {
            "Type": "bind",
            "Source": "/host/logs",
            "Destination": "/logs",
        }
    ]
    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="effective_container_isolation_invalid",
    ):
        docker_compose_manager.DockerComposeManager.inspect_effective_container(
            container, external_command_isolation=True
        )


def test_external_command_effective_container_accepts_required_and_bounded_runtime_env() -> (
    None
):
    container = _DummyContainer(
        _effective_attrs(
            mounts=[],
            env=[
                "TEST_DIR=/tests",
                "PATH=/usr/local/bin:/usr/bin",
                "LANG=C.UTF-8",
                "PYTHON_VERSION=3.13.7",
                "PYTHON_SHA256=" + "a" * 64,
                "PYTHON_GET_PIP_URL=https://github.com/pypa/get-pip/raw/"
                + "b" * 40
                + "/public/get-pip.py",
            ],
        ),
        external_command_isolation=True,
    )
    container._loopsbench_expected_compose_project = "task-client"
    container._loopsbench_expected_compose_service = "client"

    evidence = docker_compose_manager.DockerComposeManager.inspect_effective_container(
        container
    )

    assert evidence.image_id == "sha256:image"


def test_external_command_effective_container_rejects_unknown_runtime_env() -> None:
    container = _DummyContainer(
        _effective_attrs(
            mounts=[],
            env=["PATH=/usr/bin", "SERVICE_AUTH=Bearer-private-value"],
        ),
        external_command_isolation=True,
    )
    container._loopsbench_expected_compose_project = "task-client"
    container._loopsbench_expected_compose_service = "client"

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="credential_isolation_unavailable",
    ):
        docker_compose_manager.DockerComposeManager.inspect_effective_container(
            container
        )


@pytest.mark.parametrize(
    ("host_config", "networks", "network_mutation"),
    [
        ({}, {"task-client_default": {"NetworkID": "network-id"}}, None),
        (
            {},
            {
                "task-client_default": {"NetworkID": "network-id"},
                "controller_default": {"NetworkID": "controller-id"},
            },
            None,
        ),
        ({}, {"other_default": {"NetworkID": "network-id"}}, None),
        ({"NetworkMode": "bridge"}, None, None),
        ({}, None, "wrong-label"),
        ({}, None, "wrong-id"),
    ],
    ids=[
        "exact-default",
        "extra-network",
        "wrong-network",
        "unnamed-bridge-mode",
        "mislabeled-network",
        "network-id-mismatch",
    ],
)
def test_external_command_live_network_is_exact_runner_default(
    host_config: dict[str, object],
    networks: dict[str, object] | None,
    network_mutation: str | None,
) -> None:
    container = _DummyContainer(
        _effective_attrs(mounts=[], host_config=host_config, networks=networks),
        external_command_isolation=True,
    )
    container._loopsbench_expected_compose_project = "task-client"
    container._loopsbench_expected_compose_service = "client"
    if network_mutation == "wrong-label":
        container.client.networks.networks["task-client_default"].attrs["Labels"][
            "com.docker.compose.project"
        ] = "other"
    elif network_mutation == "wrong-id":
        container.client.networks.networks["task-client_default"].attrs["Id"] = (
            "different-network-id"
        )

    if networks == {"task-client_default": {"NetworkID": "network-id"}}:
        evidence = (
            docker_compose_manager.DockerComposeManager.inspect_effective_container(
                container
            )
        )
        assert evidence.container_id == "sha256:container"
    else:
        with pytest.raises(
            docker_compose_manager.ContainerAdmissionError,
            match="effective_container_network_invalid",
        ):
            docker_compose_manager.DockerComposeManager.inspect_effective_container(
                container
            )


def test_effective_container_allows_only_repo_owned_task_volume_destinations() -> None:
    mounts = [
        {
            "Type": "bind",
            "Source": "/host/logs",
            "Destination": "/logs",
        },
        {
            "Type": "bind",
            "Source": "/host/agent-logs",
            "Destination": "/agent-logs",
        },
        {
            "Type": "volume",
            "Name": "task-client_loopsbench_shared",
            "Source": ("/var/lib/docker/volumes/task-client_loopsbench_shared/_data"),
            "Destination": "/var/lib/loopsbench/.mirror",
        },
        {
            "Type": "volume",
            "Name": "task-client_loopsbench_shared",
            "Source": ("/var/lib/docker/volumes/task-client_loopsbench_shared/_data"),
            "Destination": "/shared",
        },
    ]

    evidence = docker_compose_manager.DockerComposeManager.inspect_effective_container(
        _DummyContainer(_effective_attrs(mounts=mounts))
    )

    assert evidence.container_id == "sha256:container"


@pytest.mark.parametrize(
    ("env_name", "error"),
    [
        ("OPENAI_API_KEY", "credential_isolation_unavailable"),
        ("GITHUB_TOKEN", "credential_isolation_unavailable"),
        ("KUBECONFIG", "credential_isolation_unavailable"),
        ("AWS_PROFILE", "credential_isolation_unavailable"),
        ("SSH_AUTH_SOCK", "credential_isolation_unavailable"),
        ("PGPASSWORD", "credential_isolation_unavailable"),
        ("MYSQL_PWD", "credential_isolation_unavailable"),
        ("PGPASSFILE", "credential_isolation_unavailable"),
        ("DATABASE_URL", "credential_isolation_unavailable"),
        ("APP_DATABASE_URL", "credential_isolation_unavailable"),
        ("LOOPX_STATE_FILE", "effective_container_isolation_invalid"),
        ("LOOPX_REGISTRY", "effective_container_isolation_invalid"),
        ("DOCKER_HOST", "effective_container_isolation_invalid"),
    ],
)
def test_effective_container_inspection_rejects_credential_and_controller_env(
    env_name: str, error: str
) -> None:
    with pytest.raises(docker_compose_manager.ContainerAdmissionError, match=error):
        docker_compose_manager.DockerComposeManager.inspect_effective_container(
            _DummyContainer(_effective_attrs(env=[f"{env_name}=private"]))
        )


def test_effective_container_rejects_credential_bearing_url_value() -> None:
    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="credential_isolation_unavailable",
    ):
        docker_compose_manager.DockerComposeManager.inspect_effective_container(
            _DummyContainer(
                _effective_attrs(
                    env=["SERVICE_ENDPOINT=postgresql://user:password@db/app"]
                )
            )
        )


@pytest.mark.parametrize(
    ("host_config", "case"),
    [
        ({"IpcMode": "host"}, "host-ipc"),
        ({"IpcMode": "container:peer"}, "container-ipc"),
        ({"PidMode": "host"}, "host-pid"),
        ({"PidMode": "container:peer"}, "container-pid"),
        ({"NetworkMode": "host"}, "host-network"),
        ({"NetworkMode": "container:peer"}, "container-network"),
        ({"CgroupnsMode": "host"}, "host-cgroup"),
        ({"UsernsMode": "host"}, "host-user"),
        ({"UTSMode": "host"}, "host-uts"),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_effective_container_rejects_shared_namespace_modes(
    host_config: dict[str, object],
    case: str,
) -> None:
    del case
    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="effective_container_isolation_invalid",
    ):
        docker_compose_manager.DockerComposeManager.inspect_effective_container(
            _DummyContainer(_effective_attrs(host_config=host_config))
        )


@pytest.mark.parametrize(
    "mount",
    [
        {
            "Type": "bind",
            "Source": "/host/workspace",
            "Destination": "/workspace",
        },
        {
            "Type": "bind",
            "Source": "/other/logs",
            "Destination": "/logs",
        },
        {
            "Type": "bind",
            "Source": "/run/secrets/provider-token",
            "Destination": "/logs",
        },
        {
            "Type": "bind",
            "Source": "/var/lib/docker/containers/id/mounts/configs/solver",
            "Destination": "/agent-logs",
        },
        {
            "Type": "volume",
            "Name": "external_solver_state",
            "Source": "/var/lib/docker/volumes/external_solver_state/_data",
            "Destination": "/shared",
        },
        {
            "Type": "volume",
            "Name": "task-client_traex-controller",
            "Source": "/var/lib/docker/volumes/task-client_traex-controller/_data",
            "Destination": "/shared",
        },
        {
            "Type": "volume",
            "Name": "task-client_traex-profile",
            "Source": "/var/lib/docker/volumes/task-client_traex-profile/_data",
            "Destination": "/shared",
        },
        {
            "Type": "volume",
            "Name": "task-client_loopsbench_shared",
            "Source": ("/var/lib/docker/volumes/task-client_loopsbench_shared/_data"),
            "Destination": "/opt/traex-controller",
        },
        {
            "Type": "volume",
            "Name": "task-client_loopsbench_shared",
            "Source": ("/var/lib/docker/volumes/task-client_loopsbench_shared/_data"),
            "Destination": "/opt/traex-profile",
        },
        {
            "Type": "tmpfs",
            "Source": "tmpfs",
            "Destination": "/shared",
        },
    ],
    ids=[
        "workspace-bind",
        "spoofed-log-bind-source",
        "secret-source",
        "config-source",
        "external-volume",
        "controller-volume",
        "profile-volume",
        "legacy-controller-destination",
        "legacy-profile-destination",
        "unknown-mount-type",
    ],
)
def test_effective_container_inspection_rejects_untrusted_mounts(
    mount: dict[str, object],
) -> None:
    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="effective_container_isolation_invalid",
    ):
        docker_compose_manager.DockerComposeManager.inspect_effective_container(
            _DummyContainer(_effective_attrs(mounts=[mount]))
        )


def test_effective_container_rejects_task_volume_without_compose_ownership() -> None:
    mount = {
        "Type": "volume",
        "Name": "task-client_loopsbench_shared",
        "Source": "/var/lib/docker/volumes/task-client_loopsbench_shared/_data",
        "Destination": "/shared",
    }

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="effective_container_isolation_invalid",
    ):
        docker_compose_manager.DockerComposeManager.inspect_effective_container(
            _DummyContainer(_effective_attrs(mounts=[mount], compose_project=None))
        )


def test_effective_container_rejects_task_volume_destinations_with_different_sources() -> (
    None
):
    mounts = [
        {
            "Type": "volume",
            "Name": "task-client_loopsbench_shared",
            "Source": ("/var/lib/docker/volumes/task-client_loopsbench_shared/_data"),
            "Destination": "/var/lib/loopsbench/.mirror",
        },
        {
            "Type": "volume",
            "Name": "task-client_loopsbench_shared",
            "Source": "/var/lib/docker/volumes/spoofed/_data",
            "Destination": "/shared",
        },
    ]

    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="effective_container_isolation_invalid",
    ):
        docker_compose_manager.DockerComposeManager.inspect_effective_container(
            _DummyContainer(_effective_attrs(mounts=mounts))
        )


def test_effective_container_rejects_non_task_capability() -> None:
    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="effective_container_isolation_invalid",
    ):
        docker_compose_manager.DockerComposeManager.inspect_effective_container(
            _DummyContainer(_effective_attrs(cap_add=["SYS_ADMIN"]))
        )


def test_effective_container_rejects_bind_without_recorded_runner_source() -> None:
    with pytest.raises(
        docker_compose_manager.ContainerAdmissionError,
        match="effective_container_isolation_invalid",
    ):
        docker_compose_manager.DockerComposeManager.inspect_effective_container(
            _DummyContainer(
                _effective_attrs(),
                expected_bind_sources={},
            )
        )
