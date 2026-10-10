"""Abstract base class for all agents."""

from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, Field

from loopsbench.terminal.network_policy import configured_model_hosts
from loopsbench.terminal.tmux_session import TmuxSession


class AgentResult(BaseModel):
    """Result returned by an agent after performing a task."""

    total_input_tokens: int = Field(
        default=0,
        description="Total input tokens consumed by the agent.",
    )
    total_output_tokens: int = Field(
        default=0,
        description="Total output tokens consumed by the agent.",
    )
    failure_mode: str = Field(
        default="none",
        description="Failure mode of the agent execution, if any.",
    )
    timestamped_markers: list[tuple[float, str]] = Field(
        default_factory=list,
        description="Timestamped markers from the agent execution.",
    )


class BaseAgent(ABC):
    """Base class that every agent must inherit from."""

    def __init__(self, **kwargs):
        super().__init__()
        self._version = kwargs.get("version", None)
        self._model_network_hosts = configured_model_hosts(kwargs)

    @staticmethod
    @abstractmethod
    def name() -> str:
        """Return the canonical name of this agent."""
        raise NotImplementedError

    @property
    def version(self) -> str | None:
        return getattr(self, "_version", None)

    @abstractmethod
    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
        timeout_sec: float | None = None,
    ) -> AgentResult:
        """Execute the given instruction in the provided tmux session.

        Args:
            instruction: The task instruction text.
            session: An active tmux session inside the task container.
            logging_dir: Optional directory for agent-specific logs.
            timeout_sec: Optional wall-clock budget for this agent invocation.

        Returns:
            An ``AgentResult`` with token counts and status.
        """
        raise NotImplementedError

    def install(self, session: TmuxSession, logging_dir: Path | None = None) -> object:
        """Install tools once per client container and agent class/version.

        The first installation runs before solving under the environment policy.
        Return optional host-side setup data, available as ``installation`` on
        subsequent agent instances in this trial. Failed installs are not cached.
        """

    @property
    def installation(self) -> object:
        """Setup data bound by the harness before each preparation/invocation."""
        return getattr(self, "_installation", None)

    def prepare(self, session: TmuxSession, logging_dir: Path | None = None) -> None:
        """Initialize each session/attempt, including environment and config.

        Existing custom hooks are still called on every attempt. Only the first
        preparation can use the environment policy; after solving starts, all
        subsequent preparation stays restricted. Move downloads into ``install``
        when they must run only once. No new abstract methods are required.
        """

    def model_network_hosts(self) -> list[str]:
        """Trusted plugin hook for model endpoints with nonstandard config keys.

        Return hostnames/IPs from runner configuration, never candidate files.
        Conventional model URL kwargs and environment variables are automatic.
        """
        return list(getattr(self, "_model_network_hosts", []))

    @property
    def model_uses_host_gateway(self) -> bool:
        """Whether this adapter routes model inference through Docker's gateway."""
        return False

    def get_trajectory_paths(self) -> list[str]:
        """Return container paths to trajectory/log files that should be
        extracted before the container is destroyed.

        Override in subclasses that produce trajectory files inside the
        container.  The harness will copy each path out to the trial's
        ``agent-logs/`` directory after the agent finishes.
        """
        return []
