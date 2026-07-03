"""Docker Compose lifecycle manager for Long-Horizon-Bench containers."""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generator, Mapping, Sequence

from long_horizon_bench.task_images.strategy import DockerImageStrategy
from long_horizon_bench.utils.env_model import EnvModel
from long_horizon_bench.utils.logger import logger

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


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------


def docker_cli_prefix() -> list[str]:
    return ["docker"]


def docker_env(include_os_env: bool = True) -> dict[str, str]:
    env = os.environ.copy() if include_os_env else {}
    if os.environ.get("LHB_ROOTFUL_DOCKER", "1") != "0":
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
                str(self._logs_path.absolute())
                if self._logs_path is not None
                else None
            ),
            task_agent_logs_path=(
                str(self._agent_logs_path.absolute())
                if self._agent_logs_path is not None
                else None
            ),
            task_dir=(
                str(Path(task_dir).resolve())
                if task_dir is not None
                else None
            ),
        ).to_env_dict(include_os_env=False)
        self.env = {**docker_env(), **compose_env}

    # -- compose commands --

    def _compose_cmd(self, command: list[str]) -> list[str]:
        compose_file_args: list[str] = []
        for compose_file in self._compose_files:
            compose_file_args.extend(
                ["-f", str(compose_file.resolve().absolute())]
            )
        return docker_command([
            "compose",
            "-p", self._client_container_name.lower(),
            *compose_file_args,
            *command,
        ])

    def _run_compose(self, command: list[str]) -> subprocess.CompletedProcess:
        full = self._compose_cmd(command)
        self._logger.debug(f"Running: {' '.join(full)}")
        try:
            return subprocess.run(
                full, env=self.env, check=True,
                capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as exc:
            tail = 4000
            self._logger.warning(
                f"Docker compose failed (exit {exc.returncode}): "
                f"{' '.join(full)}"
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

    def _service_container(self, service: str, retries: int = 60, delay_sec: float = 0.5) -> Container | None:
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

    def start(self) -> Container:
        if self._docker_image_strategy == DockerImageStrategy.REMOTE:
            self._pull_remote_image()
            self._run_compose(["up", "-d", "--no-build"])
        elif self._docker_image_strategy == DockerImageStrategy.LOCAL_BUILD:
            self._run_compose(["build"])
            self._run_compose(["up", "-d"])
        else:
            self._ensure_local_image_exists()
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
                    "and that LHB_TASK_DOCKER_TESTER_CONTAINER_NAME matches "
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
                    "mkdir -p /var/lib/lhb && "
                    "rm -rf /var/lib/lhb/git && "
                    "cd /workspace && "
                    "rm -rf .git && "
                    "GIT_DIR=/var/lib/lhb/git GIT_WORK_TREE=/workspace "
                    "  git init -q && "
                    "GIT_DIR=/var/lib/lhb/git GIT_WORK_TREE=/workspace "
                    "  git -c user.email=b@b -c user.name=b add -A && "
                    "GIT_DIR=/var/lib/lhb/git GIT_WORK_TREE=/workspace "
                    "  git -c user.email=b@b -c user.name=b commit -q -m base --allow-empty'"
                )
                if rc != 0:
                    self._logger.warning(
                        f"workspace git bootstrap failed (rc={rc}): "
                        f"{(out or b'').decode(errors='replace')[:300]}"
                    )
            except Exception as exc:
                self._logger.warning(f"git init in workspace failed: {exc}")
        return self._client_container

    @property
    def tester_container(self) -> Container | None:
        return self._tester_container

    def stop(self) -> None:
        try:
            self._run_compose(["down"])
            if self._cleanup:
                self._run_compose(["down", "--rmi", "all", "--volumes"])
        except Exception as exc:
            self._logger.error(f"Error stopping compose services: {exc}")

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
