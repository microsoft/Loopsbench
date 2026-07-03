"""Agent / model runtime: environment variables from YAML."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class LhbModelRuntimeFileConfig(BaseModel):
    """Values merged into ``os.environ`` before the harness runs agents.

    Precedence: process env (and prior dotenv) < ``env_files`` in order < ``env`` map.
    """

    model_config = ConfigDict(extra="ignore")

    env: dict[str, str] = Field(default_factory=dict)
    env_files: list[Path] = Field(
        default_factory=list,
        description="Optional .env paths; loaded with python-dotenv (no override of existing keys by default).",
    )
    override_process_env: bool = Field(
        default=False,
        description="If true, load_dotenv(..., override=True) for env_files; env map still wins last.",
    )
