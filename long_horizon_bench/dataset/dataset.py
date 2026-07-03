"""Dataset loading, filtering, and iteration for Long-Horizon-Bench."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Iterator

import yaml
from pydantic import BaseModel, Field, model_validator
from typing_extensions import Self

try:
    from tabulate import tabulate
except ModuleNotFoundError:
    def tabulate(rows, headers, tablefmt):
        del tablefmt
        header_line = " | ".join(str(h) for h in headers)
        body_lines = [" | ".join(str(cell) for cell in row) for row in rows]
        return "\n".join([header_line, *body_lines])

from long_horizon_bench.handlers.trial_handler import Task, TaskPaths
from long_horizon_bench.utils.logger import logger


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class DatasetConfig(BaseModel):
    """Configuration for loading a dataset."""

    name: str | None = None
    version: str | None = None

    # Path to a local dataset directory (overrides name/version)
    path: Path | None = None

    # Subsetting
    task_ids: list[str] | None = None
    n_tasks: int | None = None
    exclude_task_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_config(self) -> Self:
        if self.task_ids is not None and self.n_tasks is not None:
            raise ValueError("Cannot specify both task_ids and n_tasks")

        if self.path is None and (self.version is None or self.name is None):
            raise ValueError(
                "If path is not set, both version and name must be set"
            )
        elif self.path is not None and (
            self.version is not None or self.name is not None
        ):
            raise ValueError(
                "If path is set, version and name should not be set"
            )
        return self


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

class Dataset:
    """Load and iterate over tasks in a dataset directory."""

    def __init__(
        self,
        name: str | None = None,
        version: str | None = None,
        path: Path | None = None,
        task_ids: list[str] | None = None,
        n_tasks: int | None = None,
        exclude_task_ids: list[str] | None = None,
    ):
        """Initialise the dataset.

        Args:
            name: Dataset name (for future registry support).
            version: Dataset version (for future registry support).
            path: Path to the local task directory.
            task_ids: Optional list of task IDs or glob patterns to include.
            n_tasks: Max number of tasks to load.
            exclude_task_ids: Task IDs or glob patterns to exclude.
        """
        self.config = DatasetConfig(
            name=name,
            version=version,
            path=path,
            task_ids=task_ids,
            n_tasks=n_tasks,
            exclude_task_ids=exclude_task_ids or [],
        )

        self._logger = logger.getChild(__name__)
        self._tasks: list[Path] = []

        self._init_path()
        self._init_dataset()
        self._validate_task_paths()

    def _init_path(self) -> None:
        """Resolve the dataset path."""
        if self.config.path is not None:
            self._path = self.config.path
        else:
            raise ValueError(
                "Registry-based datasets are not yet supported. "
                "Please provide a local path."
            )

    @classmethod
    def from_yaml(cls, yaml_path: Path | str) -> Dataset:
        """Create a Dataset from a YAML config file."""
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        config = DatasetConfig(**data)
        return cls(
            name=config.name,
            version=config.version,
            path=config.path,
            task_ids=config.task_ids,
            n_tasks=config.n_tasks,
            exclude_task_ids=config.exclude_task_ids,
        )

    # -- subsetting helpers --

    def _get_included_task_ids(self) -> set[str]:
        if self.config.task_ids is None:
            # All subdirectories that are not hidden / template
            return {
                p.name
                for p in self._path.iterdir()
                if p.is_dir() and not p.name.startswith("_")
            }

        included: set[str] = set()
        for pattern in self.config.task_ids:
            matches = list(self._path.glob(pattern))
            if not matches:
                raise ValueError(f"No tasks found matching pattern: {pattern}")
            included.update(m.name for m in matches)
        return included

    def _get_excluded_task_ids(self) -> set[str]:
        if not self.config.exclude_task_ids:
            return set()
        excluded: set[str] = set()
        for pattern in self.config.exclude_task_ids:
            for p in self._path.iterdir():
                if fnmatch.fnmatch(p.name, pattern):
                    excluded.add(p.name)
        return excluded

    def _init_dataset(self) -> None:
        included = self._get_included_task_ids()
        excluded = self._get_excluded_task_ids()
        filtered = included - excluded
        self._tasks = self._limit_tasks(
            [self._path / tid for tid in sorted(filtered)]
        )

    def _validate_task_paths(self) -> None:
        for tp in self._tasks:
            if not tp.exists():
                raise FileNotFoundError(f"Task path {tp} does not exist")

    def _limit_tasks(self, task_paths: list[Path]) -> list[Path]:
        if self.config.n_tasks is None:
            return task_paths
        limited = task_paths[: self.config.n_tasks]
        if len(limited) < self.config.n_tasks:
            self._logger.warning(
                f"Requested {self.config.n_tasks} tasks but only "
                f"{len(limited)} found."
            )
        return limited

    # -- sorting --

    def sort_by_duration(self) -> None:
        """Sort tasks longest-first for optimal concurrent scheduling."""
        durations: list[tuple[Path, float]] = []
        for tp in self._tasks:
            try:
                task = Task.from_yaml(TaskPaths(tp).task_config_path)
                dur = task.effective_estimated_duration_sec
            except Exception as exc:
                self._logger.warning(
                    f"Failed to load task {tp.name}: {exc}. "
                    "Using fallback duration."
                )
                dur = 600.0
            durations.append((tp, dur))

        durations.sort(key=lambda x: x[1], reverse=True)
        self._tasks = [t for t, _ in durations]

        # Log order
        table = [
            [
                i,
                tp.name,
                f"{int(d // 60)}m {int(d % 60)}s",
            ]
            for i, (tp, d) in enumerate(durations, 1)
        ]
        self._logger.debug(
            "Task execution order (longest first):\n"
            + tabulate(table, headers=["#", "Task", "Duration"], tablefmt="grid")
        )

    # -- iteration --

    def __iter__(self) -> Iterator[Path]:
        return iter(self._tasks)

    def __len__(self) -> int:
        return len(self._tasks)

    @property
    def tasks(self) -> list[Path]:
        return self._tasks

    @property
    def task_ids(self) -> list[str]:
        return [p.name for p in self._tasks]
