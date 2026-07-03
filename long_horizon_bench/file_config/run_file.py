"""Harness run options loaded from YAML (lhb run / lhb runs create)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from long_horizon_bench.agents.agent_name import AgentName
from long_horizon_bench.task_images.strategy import DockerImageStrategy


class LhbRunFileConfig(BaseModel):
    """All fields optional; unset keys do not override harness defaults."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    dataset_path: Path | None = None
    output_path: Path | None = None
    run_id: str | None = None
    task_ids: list[str] | None = None
    n_tasks: int | None = None
    exclude_task_ids: list[str] | None = None
    no_rebuild: bool | None = None
    docker_image_strategy: DockerImageStrategy | None = None
    docker_image_namespace: str | None = None
    docker_image_tag: str | None = None
    cleanup: bool | None = None
    model: str | None = Field(default=None, description="provider/model_name")
    model_name: str | None = None
    agent: AgentName | str | None = None
    agent_import_path: str | None = None
    log_level: str | None = None
    livestream: bool | None = None
    n_concurrent: int | None = None
    n_concurrent_trials: int | None = None
    n_attempts: int | None = None
    agent_kwargs: dict[str, Any] = Field(default_factory=dict)
    global_timeout_multiplier: float | None = None
    global_agent_timeout_sec: float | None = None
    global_test_timeout_sec: float | None = None
    model_config_path: Path | None = Field(
        default=None,
        description="Path to model/runtime env YAML (overridden by --model-config).",
    )

    @field_validator("agent", mode="before")
    @classmethod
    def _coerce_agent(cls, v: Any) -> Any:
        if v is None or isinstance(v, AgentName):
            return v
        if isinstance(v, str):
            return AgentName(v)
        return v

    @field_validator("task_ids", mode="after")
    @classmethod
    def _empty_task_ids_means_all(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and len(v) == 0:
            return None
        return v

    def effective_n_concurrent(self) -> int | None:
        return self.n_concurrent_trials if self.n_concurrent_trials is not None else self.n_concurrent

    def effective_model_name(self) -> str | None:
        return self.model_name if self.model_name is not None else self.model
