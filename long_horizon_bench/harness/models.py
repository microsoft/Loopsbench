"""Data models used by the Long-Horizon-Bench harness."""

import math
import uuid
from collections import defaultdict
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import UUID4, BaseModel, Field, computed_field

from long_horizon_bench.parsers.base_parser import UnitTestStatus
from long_horizon_bench.task_images.strategy import DockerImageStrategy


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

class FailureMode(str, Enum):
    """Categorised failure modes for agent / test execution."""

    UNSET = "unset"
    NONE = "none"
    AGENT_TIMEOUT = "agent_timeout"
    AGENT_RATE_LIMITED = "agent_rate_limited"
    AGENT_AUTH_ERROR = "agent_auth_error"
    AGENT_NETWORK_ERROR = "agent_network_error"
    AGENT_SERVER_ERROR = "agent_server_error"
    AGENT_STARTUP_ERROR = "agent_startup_error"
    TEST_TIMEOUT = "test_timeout"
    CONTEXT_LENGTH_EXCEEDED = "context_length_exceeded"
    OUTPUT_LENGTH_EXCEEDED = "output_length_exceeded"
    FATAL_LLM_PARSE_ERROR = "fatal_llm_parse_error"
    PARSE_ERROR = "parse_error"
    UNKNOWN_AGENT_ERROR = "unknown_agent_error"


# ---------------------------------------------------------------------------
# Run metadata
# ---------------------------------------------------------------------------

class RunMetadata(BaseModel):
    """Metadata for a single harness run."""

    run_id: str
    uuid: str = Field(default_factory=lambda: str(uuid.uuid4()))
    dataset_path: str | None = None
    dataset_name: str | None = None
    dataset_version: str | None = None
    output_path: str
    agent_name: str
    no_rebuild: bool
    docker_image_strategy: str | None = None
    docker_image_namespace: str | None = None
    docker_image_tag: str | None = None
    cleanup: bool
    log_level: int
    task_ids: list[str] | None = None
    exclude_task_ids: list[str] | None = None
    n_tasks: int | None = None
    n_concurrent_trials: int = 4
    n_attempts: int = 1
    dataset_size: int = 0
    accuracy: float | None = None
    model_name: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    commit_hash: str = "unknown"
    username: str = "unknown"
    s3_bucket: str | None = None
    agent_kwargs: dict[str, Any] | None = None
    pass_at_k: dict[int, float | None] | None = None
    resumed_at: str | None = None


class TaskImageResolutionRecord(BaseModel):
    task_id: str
    client_image_ref: str
    image_source: str
    image_pull_performed: bool
    image_build_performed: bool


def build_run_metadata(
    *,
    run_id: str,
    dataset_path: Path | str | None,
    dataset_name: str | None,
    dataset_version: str | None,
    output_path: Path | str,
    agent_name: str,
    no_rebuild: bool,
    cleanup: bool,
    log_level: int,
    task_ids: list[str] | None,
    exclude_task_ids: list[str] | None,
    n_tasks: int | None,
    n_concurrent_trials: int,
    n_attempts: int,
    dataset_size: int,
    model_name: str | None,
    commit_hash: str,
    username: str,
    s3_bucket: str | None,
    docker_image_strategy: DockerImageStrategy | str | None,
    docker_image_namespace: str | None,
    docker_image_tag: str | None,
    start_time: str | None = None,
    agent_kwargs: dict[str, Any] | None = None,
    resumed_at: str | None = None,
) -> RunMetadata:
    strategy_value = (
        docker_image_strategy.value
        if isinstance(docker_image_strategy, DockerImageStrategy)
        else docker_image_strategy
    )
    return RunMetadata(
        run_id=run_id,
        dataset_path=str(Path(dataset_path).absolute()) if dataset_path else None,
        dataset_name=dataset_name,
        dataset_version=dataset_version,
        output_path=str(Path(output_path).absolute()),
        agent_name=agent_name,
        no_rebuild=no_rebuild,
        docker_image_strategy=strategy_value,
        docker_image_namespace=docker_image_namespace,
        docker_image_tag=docker_image_tag,
        cleanup=cleanup,
        log_level=log_level,
        task_ids=task_ids,
        exclude_task_ids=exclude_task_ids,
        n_tasks=n_tasks,
        n_concurrent_trials=n_concurrent_trials,
        n_attempts=n_attempts,
        dataset_size=dataset_size,
        model_name=model_name,
        commit_hash=commit_hash,
        username=username,
        s3_bucket=s3_bucket,
        start_time=start_time,
        agent_kwargs=agent_kwargs,
        resumed_at=resumed_at,
    )


def write_task_image_resolution(
    path: Path,
    *,
    task_id: str,
    client_image_ref: str,
    image_source: str,
    image_pull_performed: bool,
    image_build_performed: bool,
) -> TaskImageResolutionRecord:
    record = TaskImageResolutionRecord(
        task_id=task_id,
        client_image_ref=client_image_ref,
        image_source=image_source,
        image_pull_performed=image_pull_performed,
        image_build_performed=image_build_performed,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.model_dump_json(indent=2))
    return record


# ---------------------------------------------------------------------------
# Per-trial results
# ---------------------------------------------------------------------------

class RegressionResults(BaseModel):
    """Cumulative regression harness results for a single trial."""

    mode: str
    passed_tests: list[str] = Field(default_factory=list)
    failed_tests: list[str] = Field(default_factory=list)
    passed_units: list[str] = Field(default_factory=list)
    last_iteration_ts: str | None = None
    snapshot_count: int = 0


class ResultArtifact(BaseModel):
    """A raw result artifact discovered during or after test execution."""

    name: str
    kind: str
    format: str
    path: str
    found: bool = True
    required: bool = False
    parse_error: str | None = None


class TestCaseResult(BaseModel):
    """A normalized single test case result from one parsed source."""

    id: str
    status: UnitTestStatus
    source: str
    classname: str | None = None
    file: str | None = None
    message: str | None = None
    duration_sec: float | None = None
    language: str | None = None
    framework: str | None = None


class ParsedResultSet(BaseModel):
    """Normalized parsed results from one result source."""

    source_name: str
    format: str
    cases: list[TestCaseResult] = Field(default_factory=list)
    summary_counts: dict[str, int] = Field(default_factory=dict)
    raw_artifact_path: str | None = None
    is_fallback: bool = False
    is_summary_only: bool = False
    confidence: str = "case-level"


class TrialResults(BaseModel):
    """Results for a single task trial."""

    id: UUID4 = Field(default_factory=uuid.uuid4)
    trial_name: str
    task_id: str
    instruction: str
    is_resolved: bool | None = None
    failure_mode: FailureMode = FailureMode.UNSET
    parser_results: dict[str, str] | None = Field(default=None, exclude=True)
    final_test_results: dict[str, str] | None = None
    result_artifacts: list[ResultArtifact] = Field(default_factory=list, exclude=True)
    parsed_result_sets: list[ParsedResultSet] = Field(default_factory=list)
    raw_output_paths: dict[str, str] | None = Field(default=None, exclude=True)
    regression_results: RegressionResults | None = Field(default=None, exclude=True)
    recording_path: str | None = None
    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    trial_started_at: str | None = None
    trial_ended_at: str | None = None
    agent_started_at: str | None = None
    agent_ended_at: str | None = None
    test_started_at: str | None = None
    test_ended_at: str | None = None


# ---------------------------------------------------------------------------
# Aggregated benchmark results
# ---------------------------------------------------------------------------

class BenchmarkResults(BaseModel):
    """Aggregated results across all trials in a run."""

    id: UUID4 = Field(default_factory=uuid.uuid4)
    results: list[TrialResults] = []

    # -- helpers --

    def _get_task_success_counts(self) -> dict[str, list[int]]:
        counts: dict[str, list[int]] = defaultdict(list)
        for r in self.results:
            counts[r.task_id].append(1 if r.is_resolved else 0)
        return counts

    @staticmethod
    def _pass_at_k_estimator(n: int, c: int, k: int) -> float:
        """Unbiased estimator for pass@k."""
        if n - c < k:
            return 1.0
        product = 1.0
        for i in range(n - c + 1, n + 1):
            product *= 1.0 - (k / i)
        return float(1.0 - product)

    def _calculate_pass_at_k(
        self, k: int, task_counts: dict[str, list[int]]
    ) -> float:
        passes = []
        for success in task_counts.values():
            if len(success) < k:
                continue
            passes.append(self._pass_at_k_estimator(len(success), sum(success), k))
        return float(sum(passes) / len(passes)) if passes else 0.0

    # -- computed fields --

    @computed_field
    @property
    def pass_at_k(self) -> dict[int, float | None]:
        if not self.results:
            return {}
        task_counts = self._get_task_success_counts()
        min_attempts = min(len(c) for c in task_counts.values())
        k_values = sorted(
            {2**i for i in range(1, int(math.log2(max(min_attempts, 1))) + 1)}
        )
        if min_attempts >= 5:
            k_values.append(5)
        if min_attempts >= 10:
            k_values.append(10)
        return {k: self._calculate_pass_at_k(k, task_counts) for k in k_values}

    @computed_field
    @property
    def n_resolved(self) -> int:
        return len([r for r in self.results if r.is_resolved])

    @computed_field
    @property
    def n_unresolved(self) -> int:
        return len([r for r in self.results if not r.is_resolved])

    @computed_field
    @property
    def resolved_ids(self) -> list[str]:
        return [r.task_id for r in self.results if r.is_resolved]

    @computed_field
    @property
    def unresolved_ids(self) -> list[str]:
        return [r.task_id for r in self.results if not r.is_resolved]

    @computed_field
    @property
    def accuracy(self) -> float:
        if not self.results:
            return 0.0
        return self.n_resolved / len(self.results)
