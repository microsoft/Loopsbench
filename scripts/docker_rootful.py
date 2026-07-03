from __future__ import annotations

import os
import subprocess
from typing import Sequence


def docker_cli_prefix() -> list[str]:
    return ["docker"]


def docker_env() -> dict[str, str]:
    env = os.environ.copy()
    if os.environ.get("LHB_ROOTFUL_DOCKER", "1") != "0":
        env.pop("DOCKER_HOST", None)
        env.pop("DOCKER_CONTEXT", None)
    return env


def docker_cmd(*args: str) -> list[str]:
    return [*docker_cli_prefix(), *args]


def docker_run(args: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("env", docker_env())
    return subprocess.run([*docker_cli_prefix(), *args], **kwargs)
