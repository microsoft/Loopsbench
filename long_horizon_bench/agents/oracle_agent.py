"""Oracle agent that runs the reference solution."""

from pathlib import Path

import yaml

from long_horizon_bench.agents.agent_name import AgentName
from long_horizon_bench.agents.base_agent import AgentResult, BaseAgent
from long_horizon_bench.terminal.docker_compose_manager import DockerComposeManager
from long_horizon_bench.terminal.tmux_session import TmuxSession

# Path inside the container where task files (solution.sh, gold-patch.diff,
# etc.) are made available by the oracle.
_CONTAINER_TASK_DIR = "/task"
# Progressive gold shards: same layout as validate_per_pr / solution.sh under ``/workspace``.
_CONTAINER_GOLD_PATCHES_DIR = "/workspace"


class OracleAgent(BaseAgent):
    """Copies task files into the container and runs ``solution.sh``."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._dataset_path: Path | None = (
            Path(kwargs["dataset_path"]) if "dataset_path" in kwargs else None
        )
        self._task_ids: list[str] = kwargs.get("task_ids", [])

    @staticmethod
    def name() -> str:
        return AgentName.ORACLE.value

    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
        timeout_sec: float | None = None,
    ) -> AgentResult:
        if self._dataset_path is None or not self._task_ids:
            raise ValueError(
                "OracleAgent requires dataset_path and task_ids kwargs."
            )

        task_id = self._task_ids[0]
        task_dir = self._dataset_path / task_id
        solution_path = task_dir / "solution.sh"

        if not solution_path.exists():
            raise FileNotFoundError(f"Solution not found: {solution_path}")

        # Copy task-level files (solution.sh, gold-patch.diff, …) into ``/task/``.
        # Copy ``gold_patches/`` into ``/workspace/gold_patches`` so progressive
        # ``solution.sh`` matches validate_per_pr and typical ``cd /workspace`` flows.
        task_paths: list[Path] = [p for p in task_dir.iterdir() if p.is_file()]
        DockerComposeManager.copy_to_container(
            container=session._container,
            paths=task_paths,
            container_dir=_CONTAINER_TASK_DIR,
        )
        gold_shards = task_dir / "gold_patches"
        if gold_shards.is_dir():
            DockerComposeManager.copy_to_container(
                container=session._container,
                paths=[gold_shards],
                container_dir=_CONTAINER_GOLD_PATCHES_DIR,
            )

        solution_timeout = 600.0
        task_yaml = task_dir / "task.yaml"
        if task_yaml.is_file():
            try:
                meta = yaml.safe_load(task_yaml.read_text(encoding="utf-8")) or {}
                raw = float(meta.get("max_agent_timeout_sec") or 0.0)
                if raw > 0.0:
                    solution_timeout = min(raw, 86400.0)
            except (OSError, TypeError, ValueError, yaml.YAMLError):
                pass

        # Run solution.sh as a complete script so that shell constructs like
        # `set -e`, heredocs, and multi-line conditionals work correctly.
        session.send_keys(
            f"bash {_CONTAINER_TASK_DIR}/solution.sh Enter",
            block=True,
            max_timeout_sec=solution_timeout,
        )

        return AgentResult()
