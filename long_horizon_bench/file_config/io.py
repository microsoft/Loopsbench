"""Load LHB YAML config files."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from long_horizon_bench.file_config.model_runtime_file import LhbModelRuntimeFileConfig
from long_horizon_bench.file_config.run_file import LhbRunFileConfig


def load_run_file(path: Path) -> LhbRunFileConfig:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Run config must be a mapping: {path}")
    return LhbRunFileConfig.model_validate(data)


def load_model_runtime_file(path: Path) -> LhbModelRuntimeFileConfig:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Model runtime config must be a mapping: {path}")
    return LhbModelRuntimeFileConfig.model_validate(data)


def apply_model_runtime_config(cfg: LhbModelRuntimeFileConfig) -> None:
    """Apply env_files then env map to the current process (for agent subprocesses)."""
    override = cfg.override_process_env
    for env_path in cfg.env_files:
        p = Path(env_path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"Model env file not found: {p}")
        load_dotenv(p, override=override)
    for key, value in cfg.env.items():
        os.environ[key] = value


def merge_agent_kwargs(
    from_file: dict[str, Any],
    from_cli: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(from_file)
    merged.update(from_cli)
    return merged
