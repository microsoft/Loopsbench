"""Mini-SWE-Agent integration for Long-Horizon-Bench."""

import os
import shlex
from pathlib import Path

from long_horizon_bench.agents.agent_name import AgentName
from long_horizon_bench.agents.base_agent import AgentResult, BaseAgent
from long_horizon_bench.terminal.tmux_session import TmuxSession


def _get_azure_ad_token() -> str:
    """Get a fresh Azure AD token for CloudGPT via AzCli auth."""
    from long_horizon_bench.agents.mini_swe_agent.cloudgpt_aoai import (
        get_openai_token_provider,
    )

    tp = get_openai_token_provider(use_azure_cli=True, skip_access_validation=True)
    return tp()


class MiniSweAgent(BaseAgent):
    """Run tasks using the mini-swe-agent CLI."""

    _MODEL_ALIASES = {
        "azure/gpt-5.4": "azure/gpt-5.4-20260305",
    }

    _TRAJECTORY_PATH = "/tmp/mini_run.traj.json"

    def __init__(self, model_name: str, **kwargs):
        super().__init__(**kwargs)
        self._model_name = self._MODEL_ALIASES.get(model_name, model_name)
        self._provider, _ = self._model_name.split("/", 1)

    @staticmethod
    def name() -> str:
        return AgentName.MINI_SWE_AGENT.value

    def get_trajectory_paths(self) -> list[str]:
        return [self._TRAJECTORY_PATH]

    @property
    def _env_exports(self) -> list[str]:
        """Return shell export commands to set required env vars."""
        exports: list[str] = []
        exports.append("export MSWEA_CONFIGURED=true")
        exports.append("export MSWEA_COST_TRACKING=ignore_errors")

        if self._provider == "azure":
            # Azure OpenAI via CloudGPT with AAD token auth
            token = _get_azure_ad_token()
            exports.append(
                f"export AZURE_OPENAI_AD_TOKEN={shlex.quote(token)}"
            )
            azure_api_base = os.environ.get(
                "AZURE_API_BASE", "https://cloudgpt-openai.azure-api.net"
            )
            exports.append(
                f"export AZURE_API_BASE={shlex.quote(azure_api_base)}"
            )
            azure_api_version = os.environ.get(
                "AZURE_API_VERSION", "2025-04-01-preview"
            )
            exports.append(
                f"export AZURE_API_VERSION={shlex.quote(azure_api_version)}"
            )
        elif self._provider == "copilot":
            # Route A: Agent Maestro reverse proxy (OpenAI-compatible shape)
            # served on the host at PROXY_BASE (default 23335). Task containers
            # don't get host.docker.internal via extra_hosts (each task ships
            # its own docker-compose.yaml that we don't rewrite), so we have
            # the container resolve the host IP at exec time via the default
            # gateway from `ip route` and substitute it into OPENAI_API_BASE.
            tok = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get(
                "PROXY_TOK"
            )
            if not tok:
                raise ValueError(
                    "copilot provider requires ANTHROPIC_AUTH_TOKEN (or "
                    "PROXY_TOK) in the host env. Source lhb-env.sh first."
                )
            host_proxy = os.environ.get("PROXY_BASE", "http://localhost:23335")
            # Strip scheme://host:port -> port (default 23335)
            from urllib.parse import urlparse

            parsed = urlparse(host_proxy)
            port = parsed.port or 23335
            scheme = parsed.scheme or "http"
            # Detect the container's default-gateway IP from /proc/net/route
            # (avoids relying on `ip` or `host.docker.internal`, neither of
            # which is guaranteed in the task base images). The proxy on the
            # host listens on 0.0.0.0:23335 so this gateway IP reaches it.
            # We use shell hex arithmetic instead of awk's strtonum() because
            # the base images ship mawk, which doesn't have strtonum().
            gateway_expr = (
                "$(h=$(awk '$2 == \"00000000\" {print $3; exit}' /proc/net/route); "
                'printf "%d.%d.%d.%d" '
                '"0x${h:6:2}" "0x${h:4:2}" "0x${h:2:2}" "0x${h:0:2}")'
            )
            exports.append(
                f'export OPENAI_API_BASE="{scheme}://{gateway_expr}:{port}/api/openai/v1"'
            )
            exports.append(f"export OPENAI_API_KEY={shlex.quote(tok)}")
        elif "MSWEA_API_KEY" in os.environ:
            exports.append(
                f"export MSWEA_API_KEY={shlex.quote(os.environ['MSWEA_API_KEY'])}"
            )
        elif self._provider == "anthropic" and "ANTHROPIC_API_KEY" in os.environ:
            exports.append(
                f"export ANTHROPIC_API_KEY="
                f"{shlex.quote(os.environ['ANTHROPIC_API_KEY'])}"
            )
        elif self._provider == "openai" and "OPENAI_API_KEY" in os.environ:
            exports.append(
                f"export OPENAI_API_KEY="
                f"{shlex.quote(os.environ['OPENAI_API_KEY'])}"
            )
        else:
            raise ValueError(
                f"No API key found for provider {self._provider}. "
                "Please set MSWEA_API_KEY, ANTHROPIC_API_KEY, or "
                "OPENAI_API_KEY environment variable."
            )

        return exports

    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
        timeout_sec: float | None = None,
    ) -> AgentResult:
        # Provision mini-swe-agent inside the container. Two paths:
        #  1) If LHB_MINI_BUNDLE points at a tarball, copy + extract it
        #     (self-contained Python 3.11 + mini at /opt/python/bin/mini).
        #  2) Otherwise pip-install mini-swe-agent into a fresh venv at
        #     /opt/python (requires pypi reachability — verified for the
        #     standard ubuntu-24-04 / python-3-13 base images).
        bundle_host = os.environ.get("LHB_MINI_BUNDLE")
        from long_horizon_bench.terminal.docker_compose_manager import (
            DockerComposeManager,
        )

        container = session._container  # type: ignore[attr-defined]
        container.exec_run("mkdir -p /opt")

        if bundle_host and Path(bundle_host).is_file():
            DockerComposeManager.copy_to_container(
                container=container,
                paths=Path(bundle_host),
                container_dir="/opt",
                container_filename="mini-bundle.tar.gz",
            )
            session.send_keys(
                "tar -xzf /opt/mini-bundle.tar.gz -C /opt && "
                "ln -sf /opt/python/bin/mini /usr/local/bin/mini Enter",
                block=True,
                max_timeout_sec=120.0,
            )
        else:
            # Pip install path. The base images ship Debian's Python 3.12
            # without `python3-venv`, so `python3 -m venv` fails. We instead
            # install mini-swe-agent into a private prefix using pip's
            # --prefix flag. Quirk: on Debian 3.12 with the system pip's
            # distutils patch, --prefix=/opt/python actually lays the bin
            # under /opt/python/local/bin (not /opt/python/bin); we cope by
            # symlinking whichever path turned up.
            session.send_keys(
                "pip install --break-system-packages --no-cache-dir -q "
                "--prefix=/opt/python mini-swe-agent==2.2.8 && "
                "ln -sf $(ls /opt/python/local/bin/mini /opt/python/bin/mini "
                "2>/dev/null | head -1) /usr/local/bin/mini Enter",
                block=True,
                max_timeout_sec=300.0,
            )

        # Export API keys
        for export_cmd in self._env_exports:
            session.send_keys(
                f"{export_cmd} Enter",
                block=True,
                max_timeout_sec=10.0,
            )

        # Run the agent.
        # mini-swe-agent uses LiteLLM, which doesn't know the `copilot/`
        # prefix. We translate it to `openai/<m>` so LiteLLM hits the
        # OpenAI-compatible reverse proxy via OPENAI_API_BASE/OPENAI_API_KEY.
        cli_model = self._model_name
        if cli_model.startswith("copilot/"):
            cli_model = "openai/" + cli_model[len("copilot/"):]
        escaped = shlex.quote(instruction)
        # When mini-swe-agent was installed via `pip --prefix=/opt/python` the
        # console script's shebang points at /usr/bin/python3, which doesn't
        # see /opt/python/.../site-packages — set PYTHONPATH explicitly. The
        # site-packages dir lives at one of /opt/python/lib/python3.X/...
        # (older bases) or /opt/python/local/lib/python3.X/... (Debian 3.12).
        cmd = (
            "PYTHONPATH=$(ls -d /opt/python/lib/python3.*/site-packages "
            "/opt/python/local/lib/python3.*/dist-packages "
            "/opt/python/local/lib/python3.*/site-packages 2>/dev/null "
            "| paste -sd:) "
            f"/usr/local/bin/mini -m {cli_model} -t {escaped} -y"
            f" --exit-immediately --agent-class default --cost-limit 0"
            f" -o {self._TRAJECTORY_PATH}"
        )
        session.send_keys(
            f"{cmd} Enter",
            block=True,
            max_timeout_sec=86400.0,
        )

        return AgentResult()
