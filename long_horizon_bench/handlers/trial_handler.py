"""Task and trial models used by the harness.

Manages the mapping between input task directories and output trial
directories, and provides access to task metadata.
"""

from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from long_horizon_bench.parsers.parser_factory import ResultFormat
from long_horizon_bench.utils.logger import logger


# ---------------------------------------------------------------------------
# Task difficulty
# ---------------------------------------------------------------------------

class TaskDifficulty(str, Enum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"
    VERY_HARD = "very_hard"
    UNKNOWN = "unknown"

    @classmethod
    def choices(cls) -> set[str]:
        return {d.value for d in cls if d != cls.UNKNOWN}


# ---------------------------------------------------------------------------
# Task (parsed from task.yaml)
# ---------------------------------------------------------------------------

class ResultSource(BaseModel):
    """Declarative result source configuration for one task."""

    format: str
    path: str | None = None
    required: bool = False
    fallback: bool = False


class DockerConfig(BaseModel):
    """Docker configuration declared in ``task.yaml``."""

    compose_file: str = Field(default="docker-compose.yaml")


class Task(BaseModel):
    """Schema for ``task.yaml``."""

    instruction: str

    # Metadata
    author_name: str = Field(default="unknown")
    author_email: str = Field(default="unknown")
    difficulty: TaskDifficulty = Field(default=TaskDifficulty.UNKNOWN)
    category: str = Field(default="general")
    tags: list[str] = Field(default_factory=list)

    # Configuration
    parser_name: str | None = Field(default=None)
    result_sources: list[ResultSource] = Field(default_factory=list)
    docker: DockerConfig = Field(default_factory=DockerConfig)
    max_agent_timeout_sec: float = Field(default=1800.0)
    max_test_timeout_sec: float = Field(default=3600.0)
    run_tests_in_same_shell: bool = Field(default=False)
    estimated_duration_sec: float | None = Field(default=None)
    expert_time_estimate_min: int | None = Field(default=None)
    junior_time_estimate_min: int | None = Field(default=None)

    @property
    def effective_estimated_duration_sec(self) -> float:
        """Return estimated duration, falling back to an average of timeouts."""
        if self.estimated_duration_sec is not None:
            return self.estimated_duration_sec
        return (self.max_agent_timeout_sec + self.max_test_timeout_sec) / 2

    def get_result_sources(self) -> list[ResultSource]:
        """Return configured result sources with terminal-text as the default."""
        if self.result_sources:
            return self.result_sources
        return [
            ResultSource(
                format=ResultFormat.TERMINAL_TEXT.value,
                path="post-test.txt",
                fallback=True,
            )
        ]

    @classmethod
    def from_yaml(cls, path: Path) -> "Task":
        """Load a ``Task`` from a YAML file."""
        data = yaml.safe_load(path.read_text())
        try:
            return cls.model_validate(data)
        except Exception:
            logger.error(f"Error validating task at {path}")
            raise


# ---------------------------------------------------------------------------
# TaskPaths – paths inside a *source* task directory
# ---------------------------------------------------------------------------

class TaskPaths:
    """Convenience accessors for files inside a task's input directory.

    ::

        input_path/
        +-- task.yaml
        +-- solution.sh
        +-- run-tests.sh
        +-- docker-compose.yaml
        +-- Dockerfile
        +-- tests/
    """

    def __init__(self, input_path: Path):
        self.input_path = input_path

    @property
    def task_config_path(self) -> Path:
        return self.input_path / "task.yaml"

    @property
    def solution_path(self) -> Path:
        sh = self.input_path / "solution.sh"
        yml = self.input_path / "solution.yaml"
        if sh.exists():
            return sh
        if yml.exists():
            return yml
        raise FileNotFoundError(
            f"No solution.sh or solution.yaml in {self.input_path}"
        )

    @property
    def test_dir(self) -> Path:
        return self.input_path / "tests"

    @property
    def run_tests_path(self) -> Path:
        return self.input_path / "run-tests.sh"

    @property
    def docker_compose_path(self) -> Path:
        return self.input_path / "docker-compose.yaml"


# ---------------------------------------------------------------------------
# TrialPaths – paths inside an *output* trial directory
# ---------------------------------------------------------------------------

class TrialPaths:
    """Convenience accessors for files inside a trial's output directory.

    ::

        output_path/
        +-- {task_id}/
            +-- {trial_name}/
                +-- logs/
                +-- panes/
                |   +-- pre-agent.txt
                |   +-- post-agent.txt
                |   +-- post-test.txt
                +-- commands.txt
                +-- results.json
                +-- agent-logs/
    """

    def __init__(self, output_path: Path, task_id: str, trial_name: str):
        self.output_path = output_path
        self.task_id = task_id
        self.trial_name = trial_name

    @property
    def task_output_path(self) -> Path:
        return self.output_path / self.task_id / self.trial_name

    @property
    def logs_path(self) -> Path:
        return self.task_output_path / "logs"

    @property
    def panes_path(self) -> Path:
        return self.task_output_path / "panes"

    @property
    def pre_agent_pane_path(self) -> Path:
        return self.panes_path / "pre-agent.txt"

    @property
    def post_agent_pane_path(self) -> Path:
        return self.panes_path / "post-agent.txt"

    @property
    def post_test_pane_path(self) -> Path:
        return self.panes_path / "post-test.txt"

    @property
    def commands_path(self) -> Path:
        return self.task_output_path / "commands.txt"

    @property
    def results_path(self) -> Path:
        return self.task_output_path / "results.json"

    @property
    def task_image_resolution_path(self) -> Path:
        return self.task_output_path / "task_image_resolution.json"

    @property
    def agent_logging_dir(self) -> Path:
        return self.task_output_path / "agent-logs"

    @property
    def agent_plans_path(self) -> Path:
        return self.task_output_path / "agent_plans"

    @property
    def requirement_patches_path(self) -> Path:
        return self.task_output_path / "requirement_patches"

    def mkdir(self) -> None:
        """Create all output directories."""
        self.task_output_path.mkdir(parents=True, exist_ok=True)
        self.logs_path.mkdir(parents=True, exist_ok=True)
        self.panes_path.mkdir(parents=True, exist_ok=True)
        self.agent_logging_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# TrialHandler – ties task input + trial output together
# ---------------------------------------------------------------------------

class TrialHandler:
    """Combines a source task with its output trial directory."""

    def __init__(
        self,
        trial_name: str,
        input_path: Path,
        output_path: Path | None = None,
    ):
        self.trial_name = trial_name
        self._logger = logger.getChild(__name__)
        self.task_paths = TaskPaths(input_path)
        self.task = Task.from_yaml(self.task_paths.task_config_path)

        if output_path is not None:
            self.trial_paths = TrialPaths(output_path, self.task_id, trial_name)
            self.trial_paths.mkdir()

    @property
    def task_id(self) -> str:
        return self.task_paths.input_path.name

    @property
    def task_dir(self) -> Path:
        return self.task_paths.input_path

    @property
    def instruction(self) -> str:
        return self.task.instruction

    @property
    def docker_image_name_prefix(self) -> str:
        return f"lhb__{self.task_id}".replace(".", "-")

    @property
    def client_container_name(self) -> str:
        # Lowercased so that the value matches the lowercased compose project
        # name and the lowercased client_image_name. Docker compose lowercases
        # `-p <project>` internally; if our recorded name kept original case it
        # would not match the actually-created container.
        return self.trial_name.replace(".", "-").lower()

    @property
    def client_image_name(self) -> str:
        return f"{self.docker_image_name_prefix}__client".lower()

    @property
    def tester_container_name(self) -> str:
        # Must match what regression.compose.yaml constructs:
        #     ${LHB_TASK_DOCKER_CLIENT_CONTAINER_NAME}_tester
        # Previously this returned f"{client_container_name}-tester" (hyphen),
        # but the actual docker container is created with `_tester` per the
        # compose template. The mismatch broke every `docker exec
        # <tester_container_name>` call in test_runner.py / regression.
        return f"{self.client_container_name}_tester"

    @property
    def tester_image_name(self) -> str:
        return f"{self.docker_image_name_prefix}__tester".lower()

    @property
    def docker_compose_path(self) -> Path:
        compose_file = Path(self.task.docker.compose_file)
        if compose_file.is_absolute():
            raise ValueError("docker.compose_file must be a relative path")
        resolved = (self.task_dir / compose_file).resolve()
        task_dir_resolved = self.task_dir.resolve()
        if task_dir_resolved not in resolved.parents and resolved != task_dir_resolved:
            raise ValueError("docker.compose_file must stay within the task directory")
        return resolved
