"""OpenHands integration for Long-Horizon-Bench (bundled local source)."""

import io
import os
import shlex
import tarfile
from pathlib import Path

from long_horizon_bench.agents.agent_name import AgentName
from long_horizon_bench.agents.base_agent import AgentResult, BaseAgent
from long_horizon_bench.terminal.tmux_session import TmuxSession


# Path to the bundled OpenHands source tree (co-located in this repo).
_OPENHANDS_SRC = Path(__file__).parent.parent / "OpenHands"

# Directories / files to copy into the container.  Large trees (frontend,
# enterprise, tests, docs, .git) are excluded — only what is needed to build
# and run openhands-ai is copied (~7 MB total).
_INSTALL_DIRS = ("openhands", "third_party", "skills")
_INSTALL_FILES = ("pyproject.toml", "poetry.lock", "MANIFEST.in", "README.md")


class OpenHandsAgent(BaseAgent):
    """Run tasks using the bundled OpenHands source (All-Hands-AI/OpenHands).

    The agent source is installed from the local ``OpenHands/`` directory
    that lives next to this file, avoiding any network download at runtime.

    Key design choices for the new OpenHands V1-compatible implementation:
    - ``runtime = "local"`` in the TOML config selects LocalRuntime, which
      runs the action_execution_server as a subprocess directly inside the
      evaluation container (no Docker-in-Docker).
    - ``LOCAL_WORKSPACE_BASE=$LHB_WORKSPACE`` env var tells LocalRuntime to
      use the task workspace directory as-is.  In previous versions the ``-d``
      flag was used, but LocalRuntime now reads ``LOCAL_WORKSPACE_BASE`` when
      ``workspace_base`` is not set explicitly in the config.
    - ``SKIP_DEPENDENCY_CHECK=1`` bypasses the startup dependency validation
      (jupyter, libtmux) which can fail on minimal containers.
    - The package is installed in editable mode so that ``code_repo_path``
      (= parent of ``openhands.__file__``) resolves to the source tree where
      the action_execution_server can be launched correctly.
    """

    def __init__(self, model_name: str, **kwargs):
        super().__init__(**kwargs)
        # mini-style alias rewrite so callers can say azure/gpt-5.4 etc.
        aliases = {"azure/gpt-5.4": "azure/gpt-5.4-20260305"}
        self._model_name = aliases.get(model_name, model_name)
        self._provider, _ = self._model_name.split("/", 1)
        # OpenHands -> LiteLLM uses `openai/<m>` for the OpenAI-compat shape
        # served by the Agent Maestro reverse proxy. Translate `copilot/<m>`.
        self._litellm_model = self._model_name
        if self._litellm_model.startswith("copilot/"):
            self._litellm_model = "openai/" + self._litellm_model[len("copilot/"):]
        self._validate_api_key()

    @staticmethod
    def name() -> str:
        return AgentName.OPENHANDS.value

    def get_trajectory_paths(self) -> list[str]:
        return ["/root/.cache/openhands"]

    def _validate_api_key(self) -> None:
        """Raise ValueError early if no API key is available for the provider."""
        if self._provider in ("azure", "copilot"):
            return
        if "MSWEA_API_KEY" in os.environ:
            return
        if self._provider == "anthropic" and "ANTHROPIC_API_KEY" in os.environ:
            return
        if self._provider == "openai" and "OPENAI_API_KEY" in os.environ:
            return
        if "LLM_API_KEY" in os.environ:
            return
        raise ValueError(
            f"No API key found for provider '{self._provider}'. "
            "Please set LLM_API_KEY, ANTHROPIC_API_KEY, or OPENAI_API_KEY "
            "environment variable."
        )

    @property
    def _env_exports(self) -> list[str]:
        """Return shell export commands to set required env vars.

        OpenHands reads ``LLM_API_KEY`` and ``LLM_MODEL`` for its LLM backend.
        For Azure (Route B) we mint a fresh AAD token and configure LiteLLM's
        Azure provider env vars. For copilot (Route A) we point LLM_BASE_URL
        at the host's reverse-proxy via the container's default-gateway IP.
        """
        exports: list[str] = []

        if self._provider == "azure":
            # Route B: Azure CloudGPT via AAD bearer token.
            from long_horizon_bench.agents.mini_swe_agent.mini_swe_agent import (
                _get_azure_ad_token,
            )

            token = _get_azure_ad_token()
            azure_api_base = os.environ.get(
                "AZURE_API_BASE", "https://cloudgpt-openai.azure-api.net"
            )
            azure_api_version = os.environ.get(
                "AZURE_API_VERSION", "2025-04-01-preview"
            )
            exports.append(f"export AZURE_API_KEY={shlex.quote(token)}")
            exports.append(
                f"export AZURE_OPENAI_AD_TOKEN={shlex.quote(token)}"
            )
            exports.append(
                f"export AZURE_API_BASE={shlex.quote(azure_api_base)}"
            )
            exports.append(
                f"export AZURE_API_VERSION={shlex.quote(azure_api_version)}"
            )
            exports.append(f"export LLM_API_KEY={shlex.quote(token)}")
            exports.append(
                f"export LLM_MODEL={shlex.quote(self._litellm_model)}"
            )
            exports.append(f"export LLM_BASE_URL={shlex.quote(azure_api_base)}")
            exports.append(
                f"export LLM_API_VERSION={shlex.quote(azure_api_version)}"
            )
        elif self._provider == "copilot":
            # Route A: Agent Maestro reverse proxy. Same gateway-IP trick
            # as the mini-swe-agent adapter (no host.docker.internal in the
            # default task compose).
            tok = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get(
                "PROXY_TOK"
            )
            if not tok:
                raise ValueError(
                    "copilot provider requires ANTHROPIC_AUTH_TOKEN (or "
                    "PROXY_TOK) in the host env. Source lhb-env.sh first."
                )
            host_proxy = os.environ.get("PROXY_BASE", "http://localhost:23335")
            from urllib.parse import urlparse

            parsed = urlparse(host_proxy)
            port = parsed.port or 23335
            scheme = parsed.scheme or "http"
            gateway_expr = (
                "$(h=$(awk '$2 == \"00000000\" {print $3; exit}' /proc/net/route); "
                'printf "%d.%d.%d.%d" '
                '"0x${h:6:2}" "0x${h:4:2}" "0x${h:2:2}" "0x${h:0:2}")'
            )
            exports.append(f"export LLM_API_KEY={shlex.quote(tok)}")
            exports.append(
                f"export LLM_MODEL={shlex.quote(self._litellm_model)}"
            )
            exports.append(
                f'export LLM_BASE_URL="{scheme}://{gateway_expr}:{port}/api/openai/v1"'
            )
        else:
            if "MSWEA_API_KEY" in os.environ:
                api_key = os.environ["MSWEA_API_KEY"]
            elif "LLM_API_KEY" in os.environ:
                api_key = os.environ["LLM_API_KEY"]
            elif self._provider == "anthropic" and "ANTHROPIC_API_KEY" in os.environ:
                api_key = os.environ["ANTHROPIC_API_KEY"]
            elif self._provider == "openai" and "OPENAI_API_KEY" in os.environ:
                api_key = os.environ["OPENAI_API_KEY"]
            else:
                api_key = ""

            exports.append(f"export LLM_API_KEY={shlex.quote(api_key)}")
            exports.append(
                f"export LLM_MODEL={shlex.quote(self._litellm_model)}"
            )
            if "ANTHROPIC_BASE_URL" in os.environ:
                exports.append(
                    f"export LLM_BASE_URL="
                    f"{shlex.quote(os.environ['ANTHROPIC_BASE_URL'])}"
                )

        # LocalRuntime calls get_user_info() → os.getuid() = 0 (root), but
        # command.py computes user_id via `override_user_id or RUNTIME_UID or
        # 1000`.  Since 0 is falsy in Python, the fallback 1000 is used, which
        # collides with existing container users.  Fix: set RUNTIME_USERNAME=root
        # (skips useradd entirely) and RUNTIME_UID=0 (truthy string "0" breaks
        # the falsy-zero problem).
        exports.append("export RUNTIME_USERNAME=root")
        exports.append("export RUNTIME_UID=0")

        return exports

    def _copy_src_to_container(self, session: TmuxSession) -> None:
        """Copy the minimal OpenHands source tree into the container.

        Only the directories/files required for installation are included.
        The .git tree, frontend, enterprise, tests, and openhands-ui are
        excluded so the transfer stays under ~7 MB regardless of repo size.
        """
        src = _OPENHANDS_SRC
        container = session._container

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

        container.exec_run("mkdir -p /opt/oh-src")
        container.put_archive("/opt/oh-src", buf.read())

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

        # Ensure curl is available, then install uv.
        session.send_keys(
            "apt-get update -qq && apt-get install -y curl -q Enter",
            block=True,
            max_timeout_sec=60.0,
        )
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
        # OpenHands requires Python 3.12+.
        session.send_keys(
            "uv venv /opt/oh-env --python 3.12 Enter",
            block=True,
            max_timeout_sec=120.0,
        )
        session.send_keys(
            "source /opt/oh-env/bin/activate Enter",
            block=True,
            max_timeout_sec=10.0,
        )
        # Install Rust toolchain if not present — required by tiktoken (a
        # transitive dep of openhands-ai) when no pre-built wheel is available
        # (e.g. Ubuntu 16.04 containers ship without Rust).
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

        # Copy bundled OpenHands source into the container via Docker API.
        self._copy_src_to_container(session)

        # greenlet>=3 (required by playwright, a transitive dep of openhands-ai)
        # uses C++20 designated initializers, which are not supported by gcc-5
        # (Ubuntu 16.04) or gcc-7 (Ubuntu 18.04).  gcc-8+ is required.
        #
        # Strategy:
        #   1. Try to install a pre-built binary wheel via pip --only-binary.
        #      This works on systems where a compatible manylinux wheel exists.
        #   2. If that fails (no compatible binary), ensure gcc-8+ is available:
        #      - Ubuntu 20.04+ ships gcc-9 in the default repo.
        #      - Ubuntu 16.04/18.04 need the ubuntu-toolchain-r PPA for gcc-8.
        #   3. Run uv pip install with the best available C++ compiler.
        #      If greenlet was already installed as a binary in step 1, uv will
        #      skip it; otherwise CXX=g++-8 is used to compile from source.
        # If the base image's g++ is already 8+ (ubuntu-24.04 ships gcc-13),
        # we don't need to install g++-8 at all. Only attempt the apt-based
        # PPA dance if we actually need an upgrade — that path is brittle on
        # systemd-having containers and can take 10+ minutes.
        session.send_keys(
            "python -m pip install --only-binary=:all: 'greenlet>=3' 2>/dev/null; "
            "GXX_VER=$(g++ -dumpfullversion 2>/dev/null | cut -d. -f1); "
            "if [ -z \"$GXX_VER\" ] || [ \"$GXX_VER\" -lt 8 ]; then "
            "(apt-get install -y g++-8 -q 2>/dev/null || "
            "(apt-get install -y software-properties-common -qq 2>/dev/null && "
            "add-apt-repository -y ppa:ubuntu-toolchain-r/test 2>/dev/null && "
            "apt-get update -qq && apt-get install -y g++-8 -q 2>/dev/null)); "
            "fi Enter",
            block=True,
            max_timeout_sec=600.0,
        )
        session.send_keys(
            "CXX=$(which g++-8 2>/dev/null || which g++) "
            "uv pip install -e /opt/oh-src Enter",
            block=True,
            max_timeout_sec=600.0,
        )

        # Export LLM credentials into the container environment.
        for export_cmd in self._env_exports:
            session.send_keys(
                f"{export_cmd} Enter",
                block=True,
                max_timeout_sec=10.0,
            )

        # Tell LocalRuntime to use the task workspace as the working directory.
        # LocalRuntime checks LOCAL_WORKSPACE_BASE first (when workspace_base is
        # not set in config and runtime == "local"), then falls back to
        # {cwd}/workspace/local.
        session.send_keys(
            "export LOCAL_WORKSPACE_BASE=$LHB_WORKSPACE Enter",
            block=True,
            max_timeout_sec=10.0,
        )

        # Skip the startup dependency check (jupyter, libtmux validation) to
        # avoid spurious failures on minimal containers.
        session.send_keys(
            "export SKIP_DEPENDENCY_CHECK=1 Enter",
            block=True,
            max_timeout_sec=10.0,
        )

        # Write the OpenHands config file via base64 to avoid shell quoting issues.
        #
        # [core] section:
        #   runtime = "local"      — use LocalRuntime (no Docker-in-Docker)
        #   enable_browser = false — skip Playwright/Chromium init
        # [llm] section:
        #   timeout = 300          — 5-minute per-request timeout
        #   num_retries = 10       — retry up to 10 times on transient errors
        #   retry_max_wait = 120   — cap exponential backoff at 2 minutes
        oh_config = (
            '[core]\n'
            'runtime = "local"\n'
            'enable_browser = false\n'
            '\n'
            '[llm]\n'
            'timeout = 300\n'
            'num_retries = 10\n'
            'retry_max_wait = 120\n'
        )
        oh_config_b64 = __import__("base64").b64encode(oh_config.encode()).decode()
        session.send_keys(
            f"echo {shlex.quote(oh_config_b64)} | base64 -d > /tmp/oh-config.toml Enter",
            block=True,
            max_timeout_sec=10.0,
        )

        # Run OpenHands in headless mode.
        # -t: task description
        # --config-file: sets runtime = "local" and disables browser
        escaped = shlex.quote(instruction)
        cmd = (
            f"python -m openhands.core.main"
            f" -t {escaped}"
            f" --config-file /tmp/oh-config.toml"
        )
        session.send_keys(
            f"{cmd} Enter",
            block=True,
            max_timeout_sec=86400.0,  # 24 h safety ceiling
        )

        return AgentResult()
