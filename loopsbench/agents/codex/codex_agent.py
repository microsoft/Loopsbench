"""Run Codex against a Responses-compatible API in the task container.

Use OPENAI_BASE_URL / OPENAI_API_KEY or agent kwargs base_url / api_key_env.
An explicit api_key kwarg is also supported; environment variables keep keys
out of persisted run configuration. Bare model names use OpenAI by default;
provider/model names use the part after the first slash as the wire model.
The legacy copilot route retains its Agent Maestro proxy defaults.
"""

from __future__ import annotations

import io
import ipaddress
import json
import os
import platform
import re
import shlex
import shutil
import tarfile
from pathlib import Path
from urllib.parse import urlsplit

from loopsbench.agents.agent_name import AgentName
from loopsbench.agents.base_agent import AgentResult, BaseAgent
from loopsbench.terminal.network_policy import model_endpoint_hosts
from loopsbench.terminal.tmux_session import TmuxSession

_CODEX_BIN_HOST = Path(__file__).parent / "codex-rs" / "target" / "release" / "codex"
_CODEX_BIN_CONTAINER = "/usr/local/bin/codex"
_CODEX_HOME_CONTAINER = "/root/.codex"
_AUTH_DIRECTORY = "/run/loopsbench-codex"
_AUTH_ENV = "LOOPSBENCH_CODEX_API_KEY"
_TRAJECTORY_CONTAINER = "/tmp/codex_run.jsonl"
_LAST_MSG_CONTAINER = "/tmp/codex_last_message.txt"


class CodexAgent(BaseAgent):
    """Run Codex with configurable model endpoints and bearer credentials.

    ``base_url`` overrides OPENAI_BASE_URL / OPENAI_API_BASE. ``api_key``
    overrides ``api_key_env`` (OPENAI_API_KEY by default). ``query_params``
    supports provider-specific fields such as Azure's api-version.
    ``codex_binary`` or CODEX_BIN_HOST selects a native Linux executable.
    """

    def __init__(self, model_name: str, **kwargs):
        super().__init__(**kwargs)
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("Codex requires a model name")
        self._model_name = model_name.strip()
        if "/" in self._model_name:
            self._provider, self._cli_model = self._model_name.split("/", 1)
        else:
            self._provider, self._cli_model = "openai", self._model_name
        if not self._provider or not self._cli_model.strip():
            raise ValueError("Codex requires a nonempty provider and model name")

        explicit_url = (
            kwargs.get("base_url") or kwargs.get("api_base")
            or os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE")
        )
        self._legacy_proxy = self._provider == "copilot" and not explicit_url
        if explicit_url:
            base_url = explicit_url
        elif self._legacy_proxy:
            proxy = os.environ.get("PROXY_BASE", "http://localhost:23335")
            parsed = urlsplit(proxy)
            base_url = parsed._replace(path=parsed.path.rstrip("/") or "/api/openai/v1").geturl()
        elif self._provider == "openai":
            base_url = "https://api.openai.com/v1"
        else:
            raise ValueError(
                "Set base_url or OPENAI_BASE_URL to your provider's Responses API base URL"
            )
        # This validation intentionally omits the URL from error messages.
        if not isinstance(base_url, str) or not model_endpoint_hosts([base_url]):
            raise ValueError("Codex requires an HTTP(S) API base URL")
        parsed = urlsplit(base_url.strip())
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError("Use a model base URL without credentials or a fragment; configure the API key separately")
        if parsed.path.rstrip("/").endswith(("/responses", "/chat/completions")):
            raise ValueError("Set the API base URL (for example /v1), not a /responses or /chat/completions request URL")
        self._base_url = parsed._replace(path=parsed.path.rstrip("/")).geturl()
        try:
            self._uses_host_gateway = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            self._uses_host_gateway = parsed.hostname == "localhost"

        self._api_key_env = kwargs.get("api_key_env", "OPENAI_API_KEY")
        if not isinstance(self._api_key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self._api_key_env):
            raise ValueError("api_key_env must be an environment variable name")
        self._api_key = kwargs.get("api_key")
        if self._api_key is None:
            if self._legacy_proxy and "api_key_env" not in kwargs:
                self._api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("PROXY_TOK")
            else:
                self._api_key = os.environ.get(self._api_key_env)
        if not isinstance(self._api_key, str) or not self._api_key.strip():
            required = "ANTHROPIC_AUTH_TOKEN or PROXY_TOK" if self._legacy_proxy and "api_key_env" not in kwargs else self._api_key_env
            raise ValueError(f"Codex requires an API key; set {required} or provide api_key")
        if any(char in self._api_key for char in "\r\n\0"):
            raise ValueError("Codex API key must not contain control line separators")

        self._query_params = kwargs.get("query_params", {})
        if not isinstance(self._query_params, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in self._query_params.items()
        ):
            raise ValueError("query_params must map strings to strings")
        self._codex_binary = kwargs.get("codex_binary") or os.environ.get("CODEX_BIN_HOST")

    @staticmethod
    def name() -> str:
        return AgentName.CODEX.value

    def get_trajectory_paths(self) -> list[str]:
        return [_TRAJECTORY_CONTAINER, _LAST_MSG_CONTAINER, _CODEX_HOME_CONTAINER]

    def model_network_hosts(self) -> list[str]:
        return list(dict.fromkeys([*super().model_network_hosts(), *model_endpoint_hosts([self._base_url])]))

    @property
    def model_uses_host_gateway(self) -> bool:
        return self._uses_host_gateway

    def _config_toml(self, base_url: str | None = None) -> str:
        def quote(value: str) -> str:
            return json.dumps(value, ensure_ascii=False)
        config = (
            'model_provider = "loopsbench"\n'
            f'model = {quote(self._cli_model)}\n'
            'approval_policy = "never"\n'
            'sandbox_mode = "danger-full-access"\n'
            'web_search = "disabled"\n'
            '\n[model_providers.loopsbench]\n'
            'name = "LoopsBench model endpoint"\n'
            f'base_url = {quote(base_url or self._base_url)}\n'
            f'env_key = {quote(_AUTH_ENV)}\n'
            'wire_api = "responses"\n'
        )
        if self._query_params:
            config += "query_params = { " + ", ".join(
                f"{quote(key)} = {quote(value)}" for key, value in self._query_params.items()
            ) + " }\n"
        return config

    @property
    def _env_exports(self) -> list[str]:
        # Tmux commands and panes are logged. Transfer the secret through the
        # Docker API instead of embedding it in a recorded export command.
        return [f'export {_AUTH_ENV}="$(cat {_AUTH_DIRECTORY}/api-key)"']

    @staticmethod
    def _gateway(session: TmuxSession) -> str:
        container = session._container
        manager = getattr(session, "_network_controller", None)
        if manager is not None:
            controller = manager._network_controllers.get("client")
            if controller:
                container = manager._service_container(controller)
        elif container.attrs.get("HostConfig", {}).get("NetworkMode", "").startswith("container:"):
            owner = container.attrs["HostConfig"]["NetworkMode"].split(":", 1)[1]
            container = container.client.containers.get(owner)
        networks = container.attrs.get("NetworkSettings", {}).get("Networks", {}) if container else {}
        for network in networks.values():
            gateway = network.get("Gateway")
            if gateway:
                return str(ipaddress.ip_address(gateway))
        raise RuntimeError("Cannot resolve Docker host gateway for the local Codex endpoint")

    @staticmethod
    def _put_file(container, directory: str, name: str, contents: str) -> None:
        payload = contents.encode("utf-8")
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            entry = tarfile.TarInfo(name)
            entry.mode, entry.size = 0o600, len(payload)
            archive.addfile(entry, io.BytesIO(payload))
        if not container.put_archive(directory, stream.getvalue()):
            raise RuntimeError("Cannot write Codex runtime configuration")

    def _materialize_config(self, session: TmuxSession) -> None:
        base_url = self._base_url
        if self.model_uses_host_gateway:
            parsed = urlsplit(base_url)
            gateway = self._gateway(session)
            authority = f"[{gateway}]" if ":" in gateway else gateway
            if parsed.port is not None:
                authority += f":{parsed.port}"
            base_url = parsed._replace(netloc=authority).geturl()
        container = session._container
        result = container.exec_run(["mkdir", "-p", _CODEX_HOME_CONTAINER, _AUTH_DIRECTORY])
        if result.exit_code:
            raise RuntimeError("Cannot create Codex runtime directories")
        self._put_file(container, _CODEX_HOME_CONTAINER, "config.toml", self._config_toml(base_url))
        # Keep the key outside CODEX_HOME, which is collected with trajectories.
        self._put_file(container, _AUTH_DIRECTORY, "api-key", self._api_key)

    def _resolve_binary(self) -> Path:
        explicit = getattr(self, "_codex_binary", None)
        if explicit:
            path = Path(explicit).expanduser().resolve()
            if not path.is_file():
                raise RuntimeError("Configured Codex binary does not exist; check codex_binary or CODEX_BIN_HOST")
            return path
        if _CODEX_BIN_HOST.is_file():
            return _CODEX_BIN_HOST
        executable = shutil.which("codex")
        if executable:
            path = Path(executable).resolve()
            with path.open("rb") as source:
                if source.read(4) == b"\x7fELF":
                    return path
            # The npm command is a JS launcher. Copy its Linux native payload,
            # not a launcher whose node_modules would be absent in the image.
            machine = platform.machine()
            arch = {"x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
            if arch:
                triple = ("x86_64" if arch == "x64" else "aarch64") + "-unknown-linux-musl"
                package = path.parent.parent
                roots = [package / "vendor", package / "node_modules" / "@openai" / f"codex-linux-{arch}" / "vendor",
                         package.parent / f"codex-linux-{arch}" / "vendor"]
                for root in roots:
                    for suffix in ("bin/codex", "codex/codex"):
                        binary = root / triple / suffix
                        if binary.is_file():
                            return binary
        raise RuntimeError("No native Linux Codex binary found; install Codex on the host or set CODEX_BIN_HOST / codex_binary")

    def install(self, session: TmuxSession, logging_dir: Path | None = None) -> None:
        from loopsbench.terminal.docker_compose_manager import DockerComposeManager

        DockerComposeManager.copy_to_container(
            container=session._container, paths=self._resolve_binary(),
            container_dir="/usr/local/bin", container_filename="codex",
        )
        session.send_keys(f"chmod +x {_CODEX_BIN_CONTAINER} Enter", block=True, max_timeout_sec=10.0)

    def prepare(self, session: TmuxSession, logging_dir: Path | None = None) -> None:
        self._materialize_config(session)
        for export_cmd in self._env_exports:
            session.send_keys(f"{export_cmd} Enter", block=True, max_timeout_sec=10.0)

    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
        timeout_sec: float | None = None,
    ) -> AgentResult:
        cmd = (
            f"CODEX_HOME={_CODEX_HOME_CONTAINER} "
            f"{_CODEX_BIN_CONTAINER} exec "
            f"--enable goals "
            '-c web_search="disabled" '
            f"--skip-git-repo-check "
            f"--dangerously-bypass-approvals-and-sandbox "
            f"-C /workspace "
            f"--json "
            f"-o {_LAST_MSG_CONTAINER} "
            f"{shlex.quote(instruction)} > {_TRAJECTORY_CONTAINER} 2>&1"
        )
        session.send_keys(f"{cmd} Enter", block=True, max_timeout_sec=timeout_sec if timeout_sec is not None else 86400.0)
        return AgentResult()
