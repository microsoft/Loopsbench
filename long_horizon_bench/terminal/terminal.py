"""Terminal abstraction that wraps a Docker container + tmux sessions."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generator

from long_horizon_bench.task_images.strategy import DockerImageStrategy
from long_horizon_bench.terminal.docker_compose_manager import (
    DockerComposeManager,
)
from long_horizon_bench.terminal.tmux_session import TmuxSession
from long_horizon_bench.utils.logger import logger

if TYPE_CHECKING:
    from docker.models.containers import Container
else:
    Container = Any


class TerminalStartupError(RuntimeError):
    """Raised when the Docker/compose stack cannot be started for a trial."""


class Terminal:
    """Represents running containers with tmux-based sessions."""

    def __init__(
        self,
        compose_manager: DockerComposeManager,
        container: Container,
        tester_container: Container | None = None,
        logs_path: Path | None = None,
        agent_logs_path: Path | None = None,
        commands_path: Path | None = None,
    ):
        self._compose_manager = compose_manager
        self._container = container
        self._tester_container = tester_container
        self._logs_path = logs_path
        self._agent_logs_path = agent_logs_path
        self._commands_path = commands_path
        self._logger = logger.getChild(__name__)

    @property
    def compose_manager(self) -> DockerComposeManager:
        return self._compose_manager

    @property
    def tester_container(self) -> Container | None:
        return self._tester_container

    def create_session(self, name: str, use_tester: bool = False) -> TmuxSession:
        """Create a new tmux session in the selected container."""
        container = (
            self._tester_container
            if use_tester and self._tester_container is not None
            else self._container
        )
        container_name = getattr(container, "name", None)
        return TmuxSession(
            container=container,
            session_name=name,
            commands_path=self._commands_path,
            container_name=container_name,
        )

    def copy_to_container(
        self,
        paths: list[Path],
        container_dir: str,
    ) -> None:
        """Copy files into the container."""
        DockerComposeManager.copy_to_container(
            container=self._container,
            paths=paths,
            container_dir=container_dir,
        )


@contextmanager
def spin_up_terminal(
    client_container_name: str,
    client_image_name: str,
    docker_image_name_prefix: str,
    docker_compose_path: Path | list[Path],
    tester_container_name: str | None = None,
    tester_image_name: str | None = None,
    logs_path: Path | None = None,
    agent_logs_path: Path | None = None,
    commands_path: Path | None = None,
    docker_image_strategy: DockerImageStrategy | None = None,
    no_rebuild: bool = False,
    cleanup: bool = False,
    task_dir: Path | None = None,
) -> Generator[Terminal, None, None]:
    """Context manager: start compose stack -> yield Terminal -> tear down."""
    mgr = DockerComposeManager(
        client_container_name=client_container_name,
        client_image_name=client_image_name,
        tester_container_name=tester_container_name,
        tester_image_name=tester_image_name,
        docker_compose_path=docker_compose_path,
        docker_image_name_prefix=docker_image_name_prefix,
        docker_image_strategy=docker_image_strategy,
        no_rebuild=no_rebuild,
        cleanup=cleanup,
        logs_path=logs_path,
        agent_logs_path=agent_logs_path,
        task_dir=task_dir,
    )

    try:
        container = mgr.start()
    except Exception as exc:
        mgr.stop()
        raise TerminalStartupError(str(exc)) from exc
    try:
        terminal = Terminal(
            compose_manager=mgr,
            container=container,
            tester_container=mgr.tester_container,
            logs_path=logs_path,
            agent_logs_path=agent_logs_path,
            commands_path=commands_path,
        )
        yield terminal
    finally:
        mgr.stop()
