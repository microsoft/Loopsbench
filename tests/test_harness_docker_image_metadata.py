from __future__ import annotations

import logging
import sys
from contextlib import AbstractContextManager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest

from long_horizon_bench.agents.agent_name import AgentName
from long_horizon_bench.handlers.trial_handler import TrialHandler
from long_horizon_bench.harness.harness import Harness
from long_horizon_bench.harness.models import (
    FailureMode,
    build_run_metadata,
    write_task_image_resolution,
)
from long_horizon_bench.task_images.strategy import DockerImageStrategy
from long_horizon_bench.terminal.terminal import TerminalStartupError


def test_build_run_metadata_includes_docker_image_fields(tmp_path: Path) -> None:
    metadata = build_run_metadata(
        run_id="2026-07-02__12-00-00",
        dataset_path=None,
        dataset_name=None,
        dataset_version=None,
        output_path=tmp_path,
        agent_name="oracle",
        no_rebuild=False,
        cleanup=True,
        log_level=20,
        task_ids=["task_compiler"],
        exclude_task_ids=None,
        n_tasks=1,
        n_concurrent_trials=1,
        n_attempts=1,
        dataset_size=1,
        model_name="Oracle",
        commit_hash="unknown",
        username="tester",
        s3_bucket=None,
        docker_image_strategy=DockerImageStrategy.REMOTE,
        docker_image_namespace="exampleorg",
        docker_image_tag="git-ab12cd3",
    )

    assert metadata.docker_image_strategy == "remote"
    assert metadata.docker_image_namespace == "exampleorg"
    assert metadata.docker_image_tag == "git-ab12cd3"


def test_write_task_image_resolution_records_exact_image_ref(tmp_path: Path) -> None:
    out = tmp_path / "task_image_resolution.json"

    write_task_image_resolution(
        out,
        task_id="task_hadoop_seg05",
        client_image_ref="exampleorg/lhb-task-hadoop-seg05:git-ab12cd3",
        image_source="remote",
        image_pull_performed=True,
        image_build_performed=False,
    )

    payload = out.read_text()
    assert "exampleorg/lhb-task-hadoop-seg05:git-ab12cd3" in payload
    assert '"image_pull_performed": true' in payload


def test_run_trial_writes_task_image_resolution_before_terminal_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_dir = tmp_path / "tasks" / "task_compiler"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text("instruction: test instruction\n")
    (task_dir / "docker-compose.yaml").write_text("services: {}\n")

    handler = TrialHandler(
        trial_name="task_compiler.1-of-1.test_run",
        input_path=task_dir,
        output_path=tmp_path / "runs",
    )

    harness = Harness.__new__(Harness)
    harness._logger = logging.getLogger("test")
    harness._agent_name = AgentName.ORACLE
    harness._global_agent_timeout_sec = None
    harness._global_timeout_multiplier = 1.0
    harness._docker_image_strategy = DockerImageStrategy.REMOTE
    harness._docker_image_namespace = "exampleorg"
    harness._docker_image_tag = "git-ab12cd3"
    harness._no_rebuild = False
    harness._cleanup = True

    class _ExplodingTerminal(AbstractContextManager[None]):
        def __enter__(self) -> None:
            raise RuntimeError("boom")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    monkeypatch.setattr(
        "long_horizon_bench.harness.harness.spin_up_terminal",
        lambda **_: _ExplodingTerminal(),
    )

    with pytest.raises(RuntimeError, match="boom"):
        harness._run_trial(handler)

    payload = handler.trial_paths.task_image_resolution_path.read_text()
    assert "exampleorg/lhb-task-compiler:git-ab12cd3" in payload
    assert '"image_source": "remote"' in payload


def test_execute_single_trial_maps_terminal_startup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_dir = tmp_path / "tasks" / "task_compiler"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text("instruction: test instruction\n")
    (task_dir / "docker-compose.yaml").write_text("services: {}\n")

    harness = Harness.__new__(Harness)
    harness._logger = logging.getLogger("test")
    harness._output_path = tmp_path
    harness._run_id = "runs"

    def _boom(_handler: TrialHandler) -> None:
        raise TerminalStartupError("startup failed")

    monkeypatch.setattr(harness, "_run_trial", _boom)

    result = harness._execute_single_trial(
        "task_compiler.1-of-1.test_run",
        task_dir,
    )

    assert result.failure_mode == FailureMode.AGENT_STARTUP_ERROR
