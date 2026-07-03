"""Filesystem-backed LHB run and model-runtime configuration."""

from __future__ import annotations

__all__ = [
    "LhbModelRuntimeFileConfig",
    "LhbRunFileConfig",
    "apply_model_runtime_config",
    "load_model_runtime_file",
    "load_run_file",
    "merge_agent_kwargs",
    "resolve_run_params",
]


def __getattr__(name: str):
    if name in {
        "apply_model_runtime_config",
        "load_model_runtime_file",
        "load_run_file",
        "merge_agent_kwargs",
    }:
        from long_horizon_bench.file_config import io

        return getattr(io, name)
    if name == "LhbModelRuntimeFileConfig":
        from long_horizon_bench.file_config.model_runtime_file import (
            LhbModelRuntimeFileConfig,
        )

        return LhbModelRuntimeFileConfig
    if name == "LhbRunFileConfig":
        from long_horizon_bench.file_config.run_file import LhbRunFileConfig

        return LhbRunFileConfig
    if name == "resolve_run_params":
        from long_horizon_bench.file_config.resolve import resolve_run_params

        return resolve_run_params
    raise AttributeError(name)
