"""Abstract base class for all agents."""

from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, Field

from long_horizon_bench.terminal.tmux_session import TmuxSession


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

    @staticmethod
    @abstractmethod
    def name() -> str:
        """Return the canonical name of this agent."""
        raise NotImplementedError

    @property
    def version(self) -> str | None:
        return self._version

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

    def get_trajectory_paths(self) -> list[str]:
        """Return container paths to trajectory/log files that should be
        extracted before the container is destroyed.

        Override in subclasses that produce trajectory files inside the
        container.  The harness will copy each path out to the trial's
        ``agent-logs/`` directory after the agent finishes.
        """
        return []
