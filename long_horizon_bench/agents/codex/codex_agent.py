"""Codex (openai/codex) integration for Long-Horizon-Bench.

Wraps the prebuilt codex-rs binary from `codex/codex-rs/target/release/codex`,
copies it into the task container, and runs it in non-interactive `exec` mode
with goal-mode enabled. Supports the two routes used by LHB:

- copilot/<m> -> Agent Maestro reverse proxy (OpenAI Responses API on
  http://<host-gateway>:23335/api/openai/v1/responses).
- azure/<m>   -> Azure CloudGPT (Responses API) using an AAD bearer token.

Codex's wire protocol is now Responses-only; both proxy and Azure expose a
Responses endpoint that codex can hit directly.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from long_horizon_bench.agents.agent_name import AgentName
from long_horizon_bench.agents.base_agent import AgentResult, BaseAgent
from long_horizon_bench.terminal.tmux_session import TmuxSession


_CODEX_BIN_HOST = (
    Path(__file__).parent
    / "codex-rs"
    / "target"
    / "release"
    / "codex"
)
_CODEX_BIN_CONTAINER = "/usr/local/bin/codex"
_CODEX_HOME_CONTAINER = "/root/.codex"
_TRAJECTORY_CONTAINER = "/tmp/codex_run.jsonl"
_LAST_MSG_CONTAINER = "/tmp/codex_last_message.txt"


class CodexAgent(BaseAgent):
    """Run tasks using the openai/codex CLI in non-interactive exec mode."""

    _MODEL_ALIASES = {
        "azure/gpt-5.4": "azure/gpt-5.4-20260305",
    }

    def __init__(self, model_name: str, **kwargs):
        super().__init__(**kwargs)
        self._model_name = self._MODEL_ALIASES.get(model_name, model_name)
        self._provider, self._cli_model = self._model_name.split("/", 1)
        if self._provider == "azure":
            raise ValueError(
                "codex agent cannot use the azure/* route: Azure CloudGPT only "
                "exposes /chat/completions for these deployments, but codex's "
                "wire protocol is Responses-only. Use copilot/<model> instead "
                "(routes via the Agent Maestro proxy which speaks Responses)."
            )
        if self._provider != "copilot":
            raise ValueError(
                f"codex agent supports copilot/* models only, got "
                f"{self._model_name!r}"
            )

    @staticmethod
    def name() -> str:
        return AgentName.CODEX.value

    def get_trajectory_paths(self) -> list[str]:
        return [_TRAJECTORY_CONTAINER, _LAST_MSG_CONTAINER, _CODEX_HOME_CONTAINER]

    def _config_toml(self) -> str:
        """Build the codex config.toml content for the chosen provider."""
        if self._provider == "copilot":
            # The provider's base_url is materialized at exec time inside the
            # container (we substitute the gateway IP into a templated TOML).
            # Auth is via Bearer header from PROXY_TOK env var. wire_api is
            # responses (codex no longer supports chat).
            return (
                'model_provider = "lhb_proxy"\n'
                f'model = "{self._cli_model}"\n'
                'approval_policy = "never"\n'
                'sandbox_mode = "danger-full-access"\n'
                '\n'
                '[model_providers.lhb_proxy]\n'
                'name = "LHB Agent Maestro Proxy"\n'
                'base_url = "__BASE_URL__"\n'
                'env_key = "PROXY_TOK"\n'
                'wire_api = "responses"\n'
            )
        # Azure
        return (
            'model_provider = "lhb_azure"\n'
            f'model = "{self._cli_model}"\n'
            'approval_policy = "never"\n'
            'sandbox_mode = "danger-full-access"\n'
            '\n'
            '[model_providers.lhb_azure]\n'
            'name = "LHB Azure CloudGPT"\n'
            'base_url = "__BASE_URL__"\n'
            'env_key = "AZURE_OPENAI_AD_TOKEN"\n'
            'wire_api = "responses"\n'
            'query_params = { "api-version" = "__API_VERSION__" }\n'
        )

    @property
    def _env_exports(self) -> list[str]:
        exports: list[str] = []
        if self._provider == "copilot":
            tok = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get(
                "PROXY_TOK"
            )
            if not tok:
                raise ValueError(
                    "copilot provider requires ANTHROPIC_AUTH_TOKEN (or "
                    "PROXY_TOK) in the host env. Source lhb-env.sh first."
                )
            exports.append(f"export PROXY_TOK={shlex.quote(tok)}")
        else:
            from long_horizon_bench.agents.mini_swe_agent.mini_swe_agent import (
                _get_azure_ad_token,
            )

            token = _get_azure_ad_token()
            exports.append(
                f"export AZURE_OPENAI_AD_TOKEN={shlex.quote(token)}"
            )
        return exports

    def _materialize_config(self, session: TmuxSession) -> None:
        """Write codex config.toml inside the container with the right base_url.

        For copilot we resolve <gateway-ip>:23335 from /proc/net/route at
        container exec time. For azure we use AZURE_API_BASE/AZURE_API_VERSION
        from the host env.
        """
        from urllib.parse import urlparse

        if self._provider == "copilot":
            host_proxy = os.environ.get("PROXY_BASE", "http://localhost:23335")
            parsed = urlparse(host_proxy)
            port = parsed.port or 23335
            scheme = parsed.scheme or "http"
            cfg = self._config_toml()
            # Compose the final config inside the container so the gateway IP
            # is the container's own gateway, not the host's.
            session.send_keys(
                f'mkdir -p {_CODEX_HOME_CONTAINER} Enter',
                block=True,
                max_timeout_sec=5.0,
            )
            template = cfg.replace(
                "__BASE_URL__", f"{scheme}://__GW__:{port}/api/openai/v1"
            )
            import base64

            cfg_b64 = base64.b64encode(template.encode()).decode()
            session.send_keys(
                f"echo {shlex.quote(cfg_b64)} | base64 -d > /tmp/codex.toml.tpl Enter",
                block=True,
                max_timeout_sec=5.0,
            )
            # Substitute __GW__ with the container's default-gateway IP.
            session.send_keys(
                "GW=$(h=$(awk '$2 == \"00000000\" {print $3; exit}' /proc/net/route); "
                'printf "%d.%d.%d.%d" '
                '"0x${h:6:2}" "0x${h:4:2}" "0x${h:2:2}" "0x${h:0:2}"); '
                f'sed "s/__GW__/$GW/" /tmp/codex.toml.tpl > {_CODEX_HOME_CONTAINER}/config.toml Enter',
                block=True,
                max_timeout_sec=10.0,
            )
        else:
            azure_api_base = os.environ.get(
                "AZURE_API_BASE", "https://cloudgpt-openai.azure-api.net"
            )
            azure_api_version = os.environ.get(
                "AZURE_API_VERSION", "2025-04-01-preview"
            )
            base_url = (
                f"{azure_api_base}/openai/deployments/{self._cli_model}"
            )
            cfg = self._config_toml().replace("__BASE_URL__", base_url).replace(
                "__API_VERSION__", azure_api_version
            )
            import base64

            cfg_b64 = base64.b64encode(cfg.encode()).decode()
            session.send_keys(
                f'mkdir -p {_CODEX_HOME_CONTAINER} Enter',
                block=True,
                max_timeout_sec=5.0,
            )
            session.send_keys(
                f"echo {shlex.quote(cfg_b64)} | base64 -d > "
                f"{_CODEX_HOME_CONTAINER}/config.toml Enter",
                block=True,
                max_timeout_sec=5.0,
            )

    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
        timeout_sec: float | None = None,
    ) -> AgentResult:
        if not _CODEX_BIN_HOST.is_file():
            raise RuntimeError(
                f"codex binary not found at {_CODEX_BIN_HOST}. "
                "Build it with: "
                "cd long_horizon_bench/agents/codex/codex-rs && "
                "cargo build --release --bin codex"
            )

        from long_horizon_bench.terminal.docker_compose_manager import (
            DockerComposeManager,
        )

        container = session._container  # type: ignore[attr-defined]
        # Copy the prebuilt codex binary into the container.
        DockerComposeManager.copy_to_container(
            container=container,
            paths=_CODEX_BIN_HOST,
            container_dir="/usr/local/bin",
            container_filename="codex",
        )
        session.send_keys(
            f"chmod +x {_CODEX_BIN_CONTAINER} Enter",
            block=True,
            max_timeout_sec=10.0,
        )

        # Write the provider-specific config inside the container.
        self._materialize_config(session)

        # Export auth env vars.
        for export_cmd in self._env_exports:
            session.send_keys(
                f"{export_cmd} Enter",
                block=True,
                max_timeout_sec=10.0,
            )

        # Run codex in non-interactive exec mode with goal-mode enabled.
        # --skip-git-repo-check because /workspace may not be a git repo at
        # task start. --json streams events to stdout (also captured to file).
        # --output-last-message captures the final assistant message.
        # CODEX_HOME -> /root/.codex so config.toml is read.
        cmd = (
            f"CODEX_HOME={_CODEX_HOME_CONTAINER} "
            f"{_CODEX_BIN_CONTAINER} exec "
            f"--enable goals "
            f"--skip-git-repo-check "
            f"--dangerously-bypass-approvals-and-sandbox "
            f"-C /workspace "
            f"--json "
            f"-o {_LAST_MSG_CONTAINER} "
            f"{shlex.quote(instruction)} > {_TRAJECTORY_CONTAINER} 2>&1"
        )
        session.send_keys(
            f"{cmd} Enter",
            block=True,
            max_timeout_sec=86400.0,
        )

        return AgentResult()
