"""Docker Compose lifecycle manager for LoopsBench containers."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generator, Mapping, Sequence

from loopsbench.dependencies.config import (
    DependencyAccess,
    DependencyService,
    dependency_network,
)
from loopsbench.dependencies.runtime import IMAGE as DEPENDENCY_IMAGE
from loopsbench.dependencies.runtime import DependencyRuntime
from loopsbench.task_images.strategy import DockerImageStrategy
from loopsbench.terminal.network_policy import (
    NetworkPlan,
    NetworkPolicy,
    controlled_compose,
)
from loopsbench.utils.compose_env import compose_runtime_env
from loopsbench.utils.compose_security import (
    format_compose_security_issues,
    validate_compose_security,
)
from loopsbench.utils.env_model import EnvModel
from loopsbench.utils.logger import logger

if TYPE_CHECKING:
    from docker.models.containers import Container
else:
    Container = Any


_docker_module: Any | None = None


def _get_docker_module() -> Any:
    global _docker_module
    if _docker_module is None:
        import docker as docker_module

        _docker_module = docker_module
    return _docker_module


# ---------------------------------------------------------------------------
# Environment variable model (passed to docker compose)
# ---------------------------------------------------------------------------


@dataclass
class DockerComposeEnvVars(EnvModel):
    task_docker_client_container_name: str | None = None
    task_docker_client_image_name: str | None = None
    task_docker_tester_container_name: str | None = None
    task_docker_tester_image_name: str | None = None
    task_docker_name_prefix: str | None = None
    container_logs_path: str | None = None
    container_agent_logs_path: str | None = None
    test_dir: str | None = None
    task_logs_path: str | None = None
    task_agent_logs_path: str | None = None
    # Host path to the task directory; used by regression.compose.yaml bind mounts.
    task_dir: str | None = None


@dataclass(frozen=True)
class DockerImageResolution:
    requested_client_image_ref: str
    resolved_image_id: str | None = None
    resolved_repo_digest: str | None = None


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------


def docker_cli_prefix() -> list[str]:
    return ["docker"]


def docker_env(include_os_env: bool = True) -> dict[str, str]:
    env = os.environ.copy() if include_os_env else {}
    if os.environ.get("LOOPSBENCH_ROOTFUL_DOCKER", "1") != "0":
        env.pop("DOCKER_HOST", None)
        env.pop("DOCKER_CONTEXT", None)
    return env


def docker_command(args: list[str]) -> list[str]:
    return [*docker_cli_prefix(), *args]


# ---------------------------------------------------------------------------
# DockerComposeManager
# ---------------------------------------------------------------------------


class DockerComposeManager:
    """Manages the lifecycle of Docker Compose services for a single task."""

    CONTAINER_LOGS_PATH = "/logs"
    CONTAINER_AGENT_LOGS_PATH = "/agent-logs"
    CONTAINER_TEST_DIR = Path("/tests")

    def __init__(
        self,
        client_container_name: str,
        client_image_name: str,
        docker_compose_path: Path | Sequence[Path],
        docker_image_name_prefix: str | None = None,
        tester_container_name: str | None = None,
        tester_image_name: str | None = None,
        docker_image_strategy: DockerImageStrategy | None = None,
        no_rebuild: bool = False,
        cleanup: bool = False,
        logs_path: Path | None = None,
        agent_logs_path: Path | None = None,
        task_dir: Path | str | None = None,
        network: NetworkPlan | None = None,
        dependency_access: DependencyAccess | None = None,
        dependency_service: DependencyService | None = None,
    ):
        docker_module = _get_docker_module()
        try:
            self._client = docker_module.from_env(environment=docker_env())
        except docker_module.errors.DockerException as exc:
            raise RuntimeError(
                f"Error creating docker client: {exc}. "
                "Please ensure that Docker is installed and running."
            )

        self._client_container_name = client_container_name
        self._client_image_name = client_image_name
        self._tester_container_name = tester_container_name
        self._tester_image_name = tester_image_name
        self._docker_name_prefix = docker_image_name_prefix
        if isinstance(docker_compose_path, (str, Path)):
            self._compose_files = [Path(docker_compose_path)]
        else:
            self._compose_files = [Path(p) for p in docker_compose_path]
        self._docker_image_strategy = docker_image_strategy
        if self._docker_image_strategy is None:
            self._docker_image_strategy = (
                DockerImageStrategy.LOCAL_EXISTING
                if no_rebuild
                else DockerImageStrategy.LOCAL_BUILD
            )
        self._no_rebuild = no_rebuild
        self._cleanup = cleanup
        self._client_container: Container | None = None
        self._tester_container: Container | None = None
        self._logs_path = logs_path
        self._agent_logs_path = agent_logs_path
        self._logger = logger.getChild(__name__)
        self._network = network
        self._dependency_access = dependency_access
        self._dependency_service = dependency_service or DependencyService()
        self.dependencies: DependencyRuntime | None = None
        self._network_temp = None
        self._network_controllers: dict[str, str] = {}
        self._agent_network_started = False
        self._agent_installations: dict[tuple[str, type, str | None], object] = {}
        self._image_resolution = DockerImageResolution(
            requested_client_image_ref=self._client_image_name
        )

        compose_env = DockerComposeEnvVars(
            task_docker_client_image_name=self._client_image_name,
            task_docker_client_container_name=self._client_container_name,
            task_docker_tester_image_name=self._tester_image_name,
            task_docker_tester_container_name=self._tester_container_name,
            task_docker_name_prefix=self._docker_name_prefix,
            container_logs_path=self.CONTAINER_LOGS_PATH,
            container_agent_logs_path=self.CONTAINER_AGENT_LOGS_PATH,
            test_dir=str(self.CONTAINER_TEST_DIR),
            task_logs_path=(
                str(self._logs_path.absolute()) if self._logs_path is not None else None
            ),
            task_agent_logs_path=(
                str(self._agent_logs_path.absolute())
                if self._agent_logs_path is not None
                else None
            ),
            task_dir=(str(Path(task_dir).resolve()) if task_dir is not None else None),
        ).to_env_dict(include_os_env=False)
        self.env = {**docker_env(), **compose_runtime_env(compose_env)}

    # -- compose commands --

    def _compose_cmd(self, command: list[str]) -> list[str]:
        compose_file_args: list[str] = []
        for compose_file in self._compose_files:
            compose_file_args.extend(["-f", str(compose_file.resolve().absolute())])
        return docker_command(
            [
                "compose",
                "-p",
                self._client_container_name.lower(),
                *compose_file_args,
                *command,
            ]
        )

    def _run_compose(self, command: list[str]) -> subprocess.CompletedProcess:
        full = self._compose_cmd(command)
        self._logger.debug(f"Running: {' '.join(full)}")
        try:
            return subprocess.run(
                full,
                env=self.env,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            tail = 4000
            self._logger.warning(
                f"Docker compose failed (exit {exc.returncode}): {' '.join(full)}"
            )
            if exc.stdout:
                self._logger.warning(
                    f"compose stdout (tail {tail}): {(exc.stdout or '')[-tail:]}"
                )
            if exc.stderr:
                self._logger.warning(
                    f"compose stderr (tail {tail}): {(exc.stderr or '')[-tail:]}"
                )
            raise

    # -- lifecycle --

    def _service_container(
        self, service: str, retries: int = 60, delay_sec: float = 0.5
    ) -> Container | None:
        for _ in range(retries):
            result = subprocess.run(
                self._compose_cmd(["ps", "-q", service]),
                env=self.env,
                capture_output=True,
                text=True,
            )
            container_id = (result.stdout or "").strip().splitlines()
            if container_id:
                try:
                    return self._client.containers.get(container_id[0])
                except _get_docker_module().errors.NotFound:
                    pass
            time.sleep(delay_sec)
        return None

    def _run_docker(self, command: list[str]) -> subprocess.CompletedProcess:
        full = docker_command(command)
        self._logger.debug(f"Running: {' '.join(full)}")
        return subprocess.run(
            full,
            env=docker_env(),
            check=True,
            capture_output=True,
            text=True,
        )

    def _validate_task_compose_security(self) -> None:
        if not self._compose_files:
            return
        issues = validate_compose_security(self._compose_files[0])
        if issues:
            raise RuntimeError(
                "Unsafe Docker Compose task configuration:\n"
                f"{format_compose_security_issues(issues)}"
            )

    def _pull_remote_image(self) -> None:
        try:
            self._run_docker(["pull", self._client_image_name])
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            detail_suffix = f" Detail: {detail}" if detail else ""
            raise RuntimeError(
                f"Failed to pull remote task image {self._client_image_name}. "
                "Verify the image exists and that Docker can authenticate to the "
                f"registry.{detail_suffix}"
            ) from exc

    def _ensure_local_image_exists(self) -> None:
        try:
            self._run_docker(["image", "inspect", self._client_image_name])
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            detail_suffix = f" Detail: {detail}" if detail else ""
            raise RuntimeError(
                f"Task image {self._client_image_name} is not present locally for "
                "docker_image_strategy=local-existing. Use docker_image_strategy=remote, "
                "use docker_image_strategy=local-build, or pre-pull/tag the image "
                f"before running.{detail_suffix}"
            ) from exc

    @staticmethod
    def _image_repo(image_ref: str) -> str:
        if "@" in image_ref:
            return image_ref.split("@", 1)[0]
        last_slash = image_ref.rfind("/")
        last_colon = image_ref.rfind(":")
        if last_colon > last_slash:
            return image_ref[:last_colon]
        return image_ref

    @classmethod
    def _select_repo_digest(
        cls,
        image_ref: str,
        repo_digests: Sequence[str],
    ) -> str | None:
        if not repo_digests:
            return None
        requested_repo = cls._image_repo(image_ref)
        requested_prefix = f"{requested_repo}@"
        for repo_digest in repo_digests:
            if repo_digest.startswith(requested_prefix):
                return repo_digest
        return None

    def _capture_image_resolution(self) -> None:
        try:
            result = self._run_docker(
                [
                    "image",
                    "inspect",
                    "--format",
                    "{{json .}}",
                    self._client_image_name,
                ]
            )
            payload = json.loads(result.stdout or "{}")
            if not isinstance(payload, Mapping):
                raise ValueError("docker image inspect returned a non-object payload")
            image_id = payload.get("Id")
            raw_repo_digests = payload.get("RepoDigests") or []
            repo_digests = [
                repo_digest
                for repo_digest in raw_repo_digests
                if isinstance(repo_digest, str)
            ]
            self._image_resolution = DockerImageResolution(
                requested_client_image_ref=self._client_image_name,
                resolved_image_id=image_id if isinstance(image_id, str) else None,
                resolved_repo_digest=self._select_repo_digest(
                    self._client_image_name,
                    repo_digests,
                ),
            )
        except Exception as exc:
            self._logger.warning(
                "Failed to capture docker image resolution for "
                f"{self._client_image_name}: {exc}"
            )

    def start(self) -> Container:
        # Validate task-owned input before adding trusted network controllers.
        self._validate_task_compose_security()
        managed = self._dependency_access is not None and self._dependency_access.mode == "managed"
        if managed:
            # Read the actual image PATH before injecting transparent tool
            # wrappers. Building still uses the preparation network policy.
            if self._docker_image_strategy == DockerImageStrategy.REMOTE:
                self._pull_remote_image()
                # The tester may use a different remote image; its PATH must
                # also be available before dependency wrappers are composed.
                self._run_compose(["pull", "--policy", "missing", "tester"])
            elif self._docker_image_strategy == DockerImageStrategy.LOCAL_BUILD:
                self._run_compose(["build"])
            else:
                self._ensure_local_image_exists()
            self._capture_image_resolution()
        self._configure_network()
        if managed:
            self._run_compose(["up", "-d", "--no-build"])
        elif self._docker_image_strategy == DockerImageStrategy.REMOTE:
            self._pull_remote_image()
            self._capture_image_resolution()
            self._run_compose(["up", "-d", "--no-build"])
        elif self._docker_image_strategy == DockerImageStrategy.LOCAL_BUILD:
            self._run_compose(["build"])
            self._capture_image_resolution()
            self._run_compose(["up", "-d"])
        else:
            self._ensure_local_image_exists()
            self._capture_image_resolution()
            self._run_compose(["up", "-d", "--no-build"])
        self._client_container = self._service_container("client")
        if self._client_container is None:
            raise RuntimeError(
                f"Client container did not start for compose project {self._client_container_name.lower()}"
            )
        if self._tester_container_name:
            self._tester_container = self._service_container("tester")
            if self._tester_container is None:
                raise RuntimeError(
                    f"Tester container did not start for compose project "
                    f"{self._client_container_name.lower()} "
                    f"(expected name {self._tester_container_name!r}). "
                    "Check that the compose file declares a `tester` service "
                    "and that LOOPSBENCH_TASK_DOCKER_TESTER_CONTAINER_NAME matches "
                    "the container_name template in the YAML."
                )
        if self._tester_container is not None and self._client_container is not None:
            # Legacy: we used to symlink /workspace/regression_status.json ->
            # /shared/feedback/regression_status.json so the agent could read
            # live feedback. That leaked the /shared plumbing into the agent's
            # view; regression status is now host-only. Intentionally no-op.
            pass
        if self._client_container is not None:
            try:
                # Ensure git exists, then initialise a repo in /workspace so
                # diff-based progress tracking (regression_harness) can measure
                # the agent's work. Many base images lack git by default.
                rc, out = self._client_container.exec_run(
                    "sh -c '"
                    "command -v git >/dev/null 2>&1 || "
                    "(command -v apt-get >/dev/null 2>&1 && "
                    " (apt-get update -qq && apt-get install -y -qq git >/dev/null)) || "
                    "(command -v apk >/dev/null 2>&1 && apk add --no-cache git >/dev/null) || "
                    "(command -v yum >/dev/null 2>&1 && yum install -y -q git >/dev/null) || "
                    "(command -v microdnf >/dev/null 2>&1 && microdnf install -y git >/dev/null); "
                    "command -v git >/dev/null 2>&1 && "
                    "mkdir -p /var/lib/loopsbench && "
                    "rm -rf /var/lib/loopsbench/git && "
                    "cd /workspace && "
                    "rm -rf .git && "
                    "GIT_DIR=/var/lib/loopsbench/git GIT_WORK_TREE=/workspace "
                    "  git init -q && "
                    "GIT_DIR=/var/lib/loopsbench/git GIT_WORK_TREE=/workspace "
                    "  git -c user.email=b@b -c user.name=b add -A && "
                    "GIT_DIR=/var/lib/loopsbench/git GIT_WORK_TREE=/workspace "
                    "  git -c user.email=b@b -c user.name=b commit -q -m base --allow-empty'"
                )
                if rc != 0:
                    self._logger.warning(
                        f"workspace git bootstrap failed (rc={rc}): "
                        f"{(out or b'').decode(errors='replace')[:300]}"
                    )
            except Exception as exc:
                self._logger.warning(f"git init in workspace failed: {exc}")
        # Tester preparation is complete. Keep its policy independent of client
        # phase restoration while the regression worker runs concurrently.
        if self._network is not None:
            self.set_network_policy("tester", self._network.verifier)
        if self.dependencies is not None:
            self.dependencies.record_service()
            self.dependencies.initialize_client(self._client_container)
        return self._client_container

    @property
    def tester_container(self) -> Container | None:
        return self._tester_container

    @property
    def image_resolution(self) -> DockerImageResolution:
        return self._image_resolution

    def stop(self) -> None:
        try:
            self._run_compose(["down"])
            if self._cleanup:
                self._run_compose(["down", "--rmi", "all", "--volumes"])
        except Exception as exc:
            self._logger.error(f"Error stopping compose services: {exc}")
        finally:
            if self._network_temp is not None:
                self._network_temp.cleanup()
                self._network_temp = None

    def _configure_network(self) -> None:
        managed = self._dependency_access is not None and self._dependency_access.mode == "managed"
        if managed:
            self._network = self._network or NetworkPlan()
            self._network_temp = tempfile.TemporaryDirectory(prefix="loopsbench-network-")
            # Task Compose commonly exposes logs_path to the client. Gateway
            # policy/cache must live beside it, never beneath that mount.
            directory = (self._logs_path.resolve().parent / "dependencies" if self._logs_path
                         else Path(self._network_temp.name) / "dependencies")
            self.dependencies = DependencyRuntime(self._dependency_access, self._dependency_service, directory)
            self.dependencies.manager = self
            self._network = dependency_network(self._network, self._dependency_access, self.dependencies.endpoint)
        if self._network is None:
            return
        document = json.loads(self._run_compose(["config", "--format", "json"]).stdout)
        if self.dependencies is not None:
            document = self.dependencies.compose(document)
            if self._dependency_service.provider == "docker":
                self._run_docker(["build", "-t", DEPENDENCY_IMAGE, str(Path(__file__).parents[1] / "dependencies")])
        image = "loopsbench-egress:harbor-702b860-v1"
        document, self._network_controllers = controlled_compose(document, self._network, image)
        if not self._network_controllers:
            return
        # This is harness infrastructure, separate from the task-image strategy.
        self._run_docker(["build", "-t", image, str(Path(__file__).parent / "egress")])
        if self._network_temp is None:
            self._network_temp = tempfile.TemporaryDirectory(prefix="loopsbench-network-")
        path = Path(self._network_temp.name) / "compose.json"
        # Compose config already escapes literal dollars for a round trip.
        path.write_text(json.dumps(document))
        self._compose_files = [path]
        self._record_network_policy(self._network.agent_policy())

    def _record_network_policy(self, agent_policy: NetworkPolicy) -> None:
        if self._logs_path is not None and self._network is not None:
            self._logs_path.mkdir(parents=True, exist_ok=True)
            record = self._network.model_dump()
            record["agent"] = agent_policy.model_dump()
            record["scope"] = "task Docker containers; host agent controllers are outside this boundary"
            (self._logs_path / "network-policy.json").write_text(json.dumps(record, indent=2))

    def set_network_policy(self, service: str, policy: NetworkPolicy) -> None:
        if (service == "client" and self._agent_network_started
                and policy.network_mode == "public" and self._network is not None
                and self._network.agent.network_mode != "public"):
            raise ValueError("Cannot restore public client networking after solving has started")
        controller = self._network_controllers.get(service)
        if controller is None:
            return
        self._run_compose(["exec", "-T", controller, *policy.command()])

    def prepare_agent(self, agent, session, logging_dir: Path | None = None) -> None:
        # This receipt lives in the host controller, never in a candidate file.
        # New outer-loop agent objects reuse the installation in this container
        # while retaining their own per-round state and session initialization.
        key = (session._container.id, type(agent), agent.version)
        if key not in self._agent_installations:
            self._agent_installations[key] = agent.install(session=session, logging_dir=logging_dir)
        agent._installation = self._agent_installations[key]
        agent.prepare(session=session, logging_dir=logging_dir)

    @contextmanager
    def agent_network_phase(self, *, model_hosts: list[str] | None = None, uses_host_gateway: bool = False):
        """Enter solving policy, which remains active until container teardown."""
        if self._network is None:
            yield
            return
        hosts = list(model_hosts or [])
        if uses_host_gateway and self._network.agent.network_mode == "allowlist":
            # These adapters rewrite their local proxy URL to the Docker gateway.
            # Read the address from Docker, never from files/commands controlled
            # by the candidate workspace. The sidecar owns the network namespace.
            controller = self._network_controllers.get("client")
            container = self._service_container(controller) if controller else self._client_container
            networks = container.attrs.get("NetworkSettings", {}).get("Networks", {}) if container else {}
            gateways = [network["Gateway"] for network in networks.values() if network.get("Gateway")]
            if not gateways:
                raise RuntimeError("Cannot resolve Docker gateway for the configured model proxy")
            hosts.extend(gateways)
        policy = self._network.agent_policy(hosts)
        self._agent_network_started = True
        self.set_network_policy("client", policy)
        if self.dependencies is not None:
            self.dependencies.client_phase(True)
        self._record_network_policy(policy)
        # Returning, timing out, or raising must not reopen the network or
        # switch package managers back to their public installation settings.
        yield

    def build(self) -> None:
        self._run_compose(["build"])

    # -- file copy --

    @staticmethod
    def _create_tar(paths: list[Path], container_filename: str | None) -> io.BytesIO:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for p in paths:
                if p.is_file():
                    arcname = container_filename if container_filename else p.name
                    tar.add(p, arcname=arcname)
                elif p.is_dir():
                    # Preserve directory layout under ``arcname`` (e.g. ``gold_patches/*.diff``),
                    # not only basenames (which would flatten and break progressive ``solution.sh``).
                    tar.add(p, arcname=p.name, recursive=True)
                else:
                    raise ValueError(f"Path {p} is neither file nor directory")
        buf.seek(0)
        return buf

    @staticmethod
    def copy_to_container(
        container: Container,
        paths: list[Path] | Path,
        container_dir: str | None = None,
        container_filename: str | None = None,
    ) -> None:
        """Copy files / directories into a running container."""
        container_dir = container_dir or (
            container.attrs.get("Config", {}).get("WorkingDir")
            if container.attrs
            else None
        )
        if container_dir is None:
            raise ValueError("Container working directory not found")

        if isinstance(paths, Path):
            paths = [paths]

        container.exec_run(f"mkdir -p {container_dir}")
        buf = DockerComposeManager._create_tar(paths, container_filename)
        container.put_archive(container_dir, buf.read())
        buf.close()

    def copy_to_client_container(
        self,
        paths: list[Path] | Path,
        container_dir: str | None = None,
        container_filename: str | None = None,
    ) -> None:
        if self._client_container is None:
            raise ValueError("Client container not started")
        self.copy_to_container(
            container=self._client_container,
            paths=paths,
            container_dir=container_dir,
            container_filename=container_filename,
        )


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


@contextmanager
def spin_up_container(
    client_container_name: str,
    client_image_name: str,
    docker_compose_path: Path,
    docker_name_prefix: str | None = None,
    docker_image_strategy: DockerImageStrategy | None = None,
    no_rebuild: bool = False,
    cleanup: bool = False,
    logs_path: Path | None = None,
    agent_logs_path: Path | None = None,
) -> Generator[Container, None, None]:
    """Spin up a docker-compose stack, yield the client container, then tear down."""
    mgr = DockerComposeManager(
        client_container_name=client_container_name,
        client_image_name=client_image_name,
        docker_compose_path=docker_compose_path,
        docker_image_name_prefix=docker_name_prefix,
        docker_image_strategy=docker_image_strategy,
        no_rebuild=no_rebuild,
        cleanup=cleanup,
        logs_path=logs_path,
        agent_logs_path=agent_logs_path,
    )
    try:
        container = mgr.start()
        yield container
    finally:
        mgr.stop()
