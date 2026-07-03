"""SWE-agent integration for Long-Horizon-Bench (bundled local source)."""

import io
import os
import shlex
import tarfile
from pathlib import Path

from long_horizon_bench.agents.agent_name import AgentName
from long_horizon_bench.agents.base_agent import AgentResult, BaseAgent
from long_horizon_bench.terminal.tmux_session import TmuxSession


# Path to the bundled SWE-agent source tree (co-located in this repo).
_SWE_AGENT_SRC = Path(__file__).parent.parent / "swe-agent"

# Only the directories/files needed to install and run sweagent are copied to
# the container.  This avoids transferring the large tests/, docs/, assets/,
# and .git/ trees (which together account for >100 MB).
_INSTALL_DIRS = ("sweagent", "config", "tools")
_INSTALL_FILES = ("pyproject.toml", "README.md")


class SweAgent(BaseAgent):
    """Run tasks using the bundled SWE-agent source (princeton-nlp/SWE-agent).

    The agent source is installed from the local ``swe-agent/`` directory that
    lives next to this file, avoiding any network download at runtime.
    ``--env.deployment.type=local`` (via swe-rex LocalDeployment) bypasses
    SWE-agent's own Docker sandboxing so it executes code directly inside the
    existing evaluation container.
    """

    def __init__(self, model_name: str, **kwargs):
        super().__init__(**kwargs)
        self._model_name = model_name
        self._provider, self._model = model_name.split("/", 1)
        self._validate_api_key()

    @staticmethod
    def name() -> str:
        return AgentName.SWE_AGENT.value

    def get_trajectory_paths(self) -> list[str]:
        return ["/opt/swe-agent-src/trajectories"]

    def _validate_api_key(self) -> None:
        """Raise ValueError early if no API key is available for the provider."""
        if "MSWEA_API_KEY" in os.environ:
            return
        if self._provider == "anthropic" and "ANTHROPIC_API_KEY" in os.environ:
            return
        if self._provider == "openai" and "OPENAI_API_KEY" in os.environ:
            return
        raise ValueError(
            f"No API key found for provider '{self._provider}'. "
            "Please set ANTHROPIC_API_KEY or OPENAI_API_KEY environment variable."
        )

    @property
    def _env_exports(self) -> list[str]:
        """Return shell export commands to forward the required API key env vars."""
        exports: list[str] = []
        if "MSWEA_API_KEY" in os.environ:
            key_val = shlex.quote(os.environ["MSWEA_API_KEY"])
            if self._provider == "anthropic":
                exports.append(f"export ANTHROPIC_API_KEY={key_val}")
            elif self._provider == "openai":
                exports.append(f"export OPENAI_API_KEY={key_val}")
        elif self._provider == "anthropic" and "ANTHROPIC_API_KEY" in os.environ:
            exports.append(
                f"export ANTHROPIC_API_KEY="
                f"{shlex.quote(os.environ['ANTHROPIC_API_KEY'])}"
            )
            if "ANTHROPIC_BASE_URL" in os.environ:
                exports.append(
                    f"export ANTHROPIC_BASE_URL="
                    f"{shlex.quote(os.environ['ANTHROPIC_BASE_URL'])}"
                )
        elif self._provider == "openai" and "OPENAI_API_KEY" in os.environ:
            exports.append(
                f"export OPENAI_API_KEY="
                f"{shlex.quote(os.environ['OPENAI_API_KEY'])}"
            )
        return exports

    def _copy_src_to_container(self, session: TmuxSession) -> None:
        """Copy the minimal SWE-agent source tree into the container.

        Only the directories/files required for installation are included.
        The .git tree, tests, docs, and assets are excluded so the transfer
        stays under ~3 MB regardless of the repo's full size.
        """
        src = _SWE_AGENT_SRC
        container = session._container  # DockerComposeManager-provided Container

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for dir_name in _INSTALL_DIRS:
                dir_path = src / dir_name
                if not dir_path.exists():
                    continue
                for item in dir_path.rglob("*"):
                    if "__pycache__" in item.parts:
                        continue
                    if item.is_file():
                        tar.add(item, arcname=str(item.relative_to(src)))
            for file_name in _INSTALL_FILES:
                file_path = src / file_name
                if file_path.is_file():
                    tar.add(file_path, arcname=file_name)
        buf.seek(0)

        container.exec_run("mkdir -p /opt/swe-agent-src")
        container.put_archive("/opt/swe-agent-src", buf.read())
        # sweagent/__init__.py asserts TRAJECTORY_DIR.is_dir() at import time.
        # Create the directory so the assertion passes.
        container.exec_run("mkdir -p /opt/swe-agent-src/trajectories")

    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
        timeout_sec: float | None = None,
    ) -> AgentResult:
        # Capture working directory set by the task Dockerfile's WORKDIR.
        session.send_keys(
            "export LHB_WORKSPACE=$(pwd) Enter",
            block=True,
            max_timeout_sec=10.0,
        )

        # Install git (required for git init) and curl (for uv installer).
        session.send_keys(
            "apt-get update -qq && apt-get install -y git curl -q Enter",
            block=True,
            max_timeout_sec=120.0,
        )

        # SWE-agent needs a clean git repository at the workspace path.
        # Remove any nested .git dirs first so the top-level repo covers all
        # files uniformly, then commit everything so the repo is not dirty.
        session.send_keys(
            "find $LHB_WORKSPACE -mindepth 2 -maxdepth 3 -name .git -type d "
            r"-exec rm -rf {} + 2>/dev/null; "
            "git config --global user.email lhb@lhb.io && "
            "git config --global user.name LHB && "
            "git -C $LHB_WORKSPACE init -q && "
            "git -C $LHB_WORKSPACE add -A && "
            "git -C $LHB_WORKSPACE commit -q -m initial Enter",
            block=True,
            max_timeout_sec=60.0,
        )

        # Copy bundled SWE-agent source into the container via Docker API.
        self._copy_src_to_container(session)

        # Install uv (fast Python package manager).
        session.send_keys(
            "curl -LsSf https://astral.sh/uv/install.sh | sh Enter",
            block=True,
            max_timeout_sec=60.0,
        )
        session.send_keys(
            "source $HOME/.local/bin/env Enter",
            block=True,
            max_timeout_sec=10.0,
        )
        # Create a dedicated Python 3.12 venv and install sweagent from local
        # source in editable mode so that CONFIG_DIR / TOOLS_DIR resolve
        # correctly.  Python 3.12 is pinned for compatibility with sweagent's
        # dependencies (tiktoken requires Rust, which is installed below).
        session.send_keys(
            "uv venv /opt/sweagent-env --python 3.12 Enter",
            block=True,
            max_timeout_sec=60.0,
        )
        session.send_keys(
            "source /opt/sweagent-env/bin/activate Enter",
            block=True,
            max_timeout_sec=10.0,
        )
        # tiktoken (a transitive dep via litellm) requires Rust to compile from
        # source when no pre-built wheel is available.  Install rustup if rustc
        # is not already present.
        session.send_keys(
            "command -v rustc >/dev/null 2>&1 || "
            "(curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs "
            "| sh -s -- -y && source $HOME/.cargo/env) Enter",
            block=True,
            max_timeout_sec=300.0,
        )
        session.send_keys(
            "source $HOME/.cargo/env 2>/dev/null || true Enter",
            block=True,
            max_timeout_sec=10.0,
        )
        session.send_keys(
            "uv pip install -e /opt/swe-agent-src Enter",
            block=True,
            max_timeout_sec=600.0,
        )

        # Anthropic rejects requests that specify both temperature and top_p.
        # SWE-agent defaults: temperature=0.0 AND top_p=1.0.  Remove the
        # top_p keyword argument from the litellm call.
        session.send_keys(
            "sed -i '/top_p=self.config.top_p/d' "
            "/opt/swe-agent-src/sweagent/agent/models.py Enter",
            block=True,
            max_timeout_sec=10.0,
        )

        # Export API keys into the container environment.
        for export_cmd in self._env_exports:
            session.send_keys(
                f"{export_cmd} Enter",
                block=True,
                max_timeout_sec=10.0,
            )

        # Write instruction to a file to avoid shell-quoting / length issues.
        session.send_keys(
            f"printf '%s' {shlex.quote(instruction)} > /tmp/lhb-instruction.txt Enter",
            block=True,
            max_timeout_sec=10.0,
        )

        # Stage the workspace under /opt/sweagent-workspace.
        # SWE-agent (via swe-rex LocalDeployment) uploads --env.repo.path to
        # /{basename}, so /opt/sweagent-workspace → /sweagent-workspace.
        # Using a path that does not collide with existing root-level dirs.
        session.send_keys(
            "cp -r $LHB_WORKSPACE /opt/sweagent-workspace Enter",
            block=True,
            max_timeout_sec=60.0,
        )

        # Run SWE-agent in local-deployment mode.
        #   --env.deployment.type=local  : use swe-rex LocalDeployment (no Docker)
        #   --env.repo.path              : staged workspace with the clean git repo
        #   --problem_statement.type=text_file + .path : instruction from file
        cmd = (
            "sweagent run"
            " --env.deployment.type=local"
            " --env.repo.path=/opt/sweagent-workspace"
            f" --agent.model.name={shlex.quote(self._model)}"
            " --problem_statement.type=text_file"
            " --problem_statement.path=/tmp/lhb-instruction.txt"
        )
        session.send_keys(
            f"{cmd} Enter",
            block=True,
            max_timeout_sec=86400.0,  # 24 h safety ceiling
        )

        # Sync agent's edits from /sweagent-workspace back to the original
        # workspace so the test runner finds the modified files there.
        session.send_keys(
            "cp -a /sweagent-workspace/. $LHB_WORKSPACE/ Enter",
            block=True,
            max_timeout_sec=300.0,
        )

        return AgentResult()
