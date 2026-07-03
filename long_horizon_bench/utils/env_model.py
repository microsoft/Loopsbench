"""Helper to convert a model into docker-compose compatible environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields


@dataclass
class EnvModel:
    """Base dataclass whose fields are mapped to ``LHB_*`` environment variables.

    Field names are converted to uppercase and prefixed with ``LHB_`` when
    exported.  For example, a field called ``task_logs_path`` becomes
    ``LHB_TASK_LOGS_PATH``.
    """

    ENV_PREFIX: str = "LHB_"

    def to_env_dict(self, include_os_env: bool = False) -> dict[str, str]:
        """Return a ``{VAR_NAME: value}`` dict suitable for ``subprocess.run(env=...)``.

        Args:
            include_os_env: If *True*, the returned dict also contains
                the current ``os.environ`` so that inherited variables
                (``PATH``, etc.) are preserved.
        """
        env: dict[str, str] = {}

        if include_os_env:
            env.update(os.environ)

        for f in fields(self):
            if f.name == "ENV_PREFIX":
                continue
            value = getattr(self, f.name)
            if value is not None:
                key = f"{self.ENV_PREFIX}{f.name.upper()}"
                env[key] = str(value)

        return env
