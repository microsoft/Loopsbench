"""Claude Code integration for Long-Horizon-Bench."""

import json
import os
from pathlib import Path
from time import time
from typing import Any

import httpx

from long_horizon_bench.agents.agent_name import AgentName
from long_horizon_bench.agents.base_agent import AgentResult, BaseAgent
from long_horizon_bench.agents.claude_code.sdk_runner import (
    ContainerWorkspaceBackend,
    WorkspaceTools,
    run_task,
)
from long_horizon_bench.agents.mini_swe_agent.mini_swe_agent import _get_azure_ad_token
from long_horizon_bench.terminal.tmux_session import TmuxSession


class ClaudeCodeAgent(BaseAgent):
    """Run benchmark tasks through the local SDK runner on the host."""

    _MODEL_ALIASES = {
        "azure/gpt-5.4": "azure/gpt-5.4-20260305",
    }
    _COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
    _COPILOT_API_BASE = "https://api.githubcopilot.com"
    _COPILOT_TOKEN_CACHE: dict[str, Any] | None = None

    _TRAJECTORY_FILENAME = "claude_sdk_trajectory.jsonl"
    _RESULT_FILENAME = "claude_sdk_result.json"

    def __init__(self, model_name: str, **kwargs):
        super().__init__(**kwargs)
        self._model_name = self._MODEL_ALIASES.get(model_name, model_name)
        self._provider, self._model = self._model_name.split("/", 1)
        self._validate_credentials()

    @staticmethod
    def name() -> str:
        return AgentName.CLAUDE_CODE.value

    def get_trajectory_paths(self) -> list[str]:
        return []

    @classmethod
    def _get_copilot_access_token(cls) -> str:
        token = os.environ.get("GITHUB_COPILOT_ACCESS_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            return token
        raise ValueError(
            "No GitHub token found for Copilot provider. "
            "Set GITHUB_COPILOT_ACCESS_TOKEN or GH_TOKEN."
        )

    @staticmethod
    def _discover_running_copilot_shim(proc_root: Path = Path("/proc")) -> dict[str, str] | None:
        if not proc_root.exists():
            return None
        for proc_dir in proc_root.iterdir():
            if not proc_dir.name.isdigit():
                continue
            try:
                cmdline = (proc_dir / "cmdline").read_bytes().decode(errors="ignore").split("\0")
            except OSError:
                continue
            if not any("copilot_chat_shim.py" in arg for arg in cmdline):
                continue
            try:
                environ = (proc_dir / "environ").read_bytes().decode(errors="ignore").split("\0")
            except OSError:
                continue
            token = None
            for entry in environ:
                if entry.startswith("COPILOT_SHIM_TOKEN="):
                    token = entry.split("=", 1)[1]
                    break
            if not token:
                continue
            host = "127.0.0.1"
            port = "4152"
            upstream = None
            for index, arg in enumerate(cmdline):
                if arg == "--host" and index + 1 < len(cmdline):
                    host = cmdline[index + 1] or host
                elif arg == "--port" and index + 1 < len(cmdline):
                    port = cmdline[index + 1] or port
                elif arg == "--upstream" and index + 1 < len(cmdline):
                    upstream = cmdline[index + 1] or upstream
            if isinstance(upstream, str) and upstream:
                api_base = upstream.rstrip("/")
            else:
                if host in {"0.0.0.0", "::"}:
                    host = "127.0.0.1"
                api_base = f"http://{host}:{port}/v1"
            return {
                "api_key": token,
                "api_base": api_base,
            }
        return None

    @classmethod
    def _get_copilot_direct_session(cls) -> dict[str, str] | None:
        api_key = os.environ.get("GITHUB_COPILOT_API_KEY") or os.environ.get(
            "COPILOT_SHIM_TOKEN"
        )
        api_base = os.environ.get("GITHUB_COPILOT_API_BASE") or os.environ.get(
            "COPILOT_SHIM_BASE_URL"
        )
        if not api_base and api_key == os.environ.get("COPILOT_SHIM_TOKEN"):
            api_base = "http://127.0.0.1:4152/v1"
        if api_key and api_base:
            return {
                "api_key": api_key,
                "api_base": api_base.rstrip("/"),
            }
        return cls._discover_running_copilot_shim()

    @classmethod
    def _get_copilot_session(cls) -> dict[str, str]:
        direct = cls._get_copilot_direct_session()
        if direct is not None:
            return direct

        cached = cls._COPILOT_TOKEN_CACHE
        now = time()
        if cached and float(cached["expires_at"]) > now + 300:
            return {
                "api_key": str(cached["api_key"]),
                "api_base": str(cached["api_base"]),
            }

        access_token = cls._get_copilot_access_token()
        response = httpx.get(
            cls._COPILOT_TOKEN_URL,
            headers={
                "Authorization": f"token {access_token}",
                "X-GitHub-Api-Version": os.environ.get(
                    "GITHUB_COPILOT_TOKEN_API_VERSION", "2025-04-01"
                ),
            },
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        api_key = payload.get("token")
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("Copilot token exchange did not return a token")

        api_base = os.environ.get("GITHUB_COPILOT_API_BASE")
        if not api_base:
            endpoints = payload.get("endpoints")
            if isinstance(endpoints, dict):
                api_base = endpoints.get("api")
        if not isinstance(api_base, str) or not api_base:
            api_base = cls._COPILOT_API_BASE

        expires_at = payload.get("expires_at")
        if not isinstance(expires_at, (int, float)):
            expires_at = now + 1500

        cls._COPILOT_TOKEN_CACHE = {
            "api_key": api_key,
            "api_base": api_base.rstrip("/"),
            "expires_at": float(expires_at),
        }
        return {
            "api_key": api_key,
            "api_base": api_base.rstrip("/"),
        }

    def _validate_credentials(self) -> None:
        """Raise ValueError early if prerequisites are not met."""
        if self._provider == "azure":
            return
        if self._provider == "copilot":
            if self._get_copilot_direct_session() is not None:
                return
            self._get_copilot_access_token()
            return
        if self._provider != "anthropic":
            raise ValueError(
                f"ClaudeCodeAgent only supports the 'anthropic', 'azure', or 'copilot' provider, "
                f"got '{self._provider}'. "
                "Use a model name like 'anthropic/claude-opus-4-7', 'azure/gpt-5.4', or 'copilot/claude-sonnet-4'."
            )
        if "MSWEA_API_KEY" in os.environ or "ANTHROPIC_API_KEY" in os.environ:
            return
        if "ANTHROPIC_BASE_URL" in os.environ and "ANTHROPIC_AUTH_TOKEN" in os.environ:
            return
        raise ValueError(
            "No Anthropic credentials found for ClaudeCodeAgent. "
            "Set ANTHROPIC_API_KEY or MSWEA_API_KEY, or provide "
            "ANTHROPIC_BASE_URL with ANTHROPIC_AUTH_TOKEN for a local SDK proxy."
        )

    def _sdk_kwargs(self) -> dict[str, object]:
        """Auth/endpoint kwargs to forward to ``run_task``.

        Previously this was implemented by pushing values into ``os.environ``
        inside a context manager and reading them back inside ``run_task``.
        That mutated *global* state in a ThreadPoolExecutor-based concurrent
        run (see harness.run_dataset): two trials would race on
        ``AZURE_OPENAI_AD_TOKEN`` / ``GITHUB_COPILOT_API_KEY``, and one
        trial's ``finally`` could clear/overwrite the var while the other was
        mid-call, producing spurious 401s. Passing the values explicitly
        avoids the race entirely.
        """
        kwargs: dict[str, object] = {}
        if self._provider == "azure":
            kwargs["azure_ad_token_provider"] = _get_azure_ad_token
            kwargs["api_base"] = os.environ.get(
                "AZURE_API_BASE", "https://cloudgpt-openai.azure-api.net"
            )
            kwargs["api_version"] = os.environ.get(
                "AZURE_API_VERSION", "2025-04-01-preview"
            )
        elif self._provider == "copilot":
            session = self._get_copilot_session()
            kwargs["copilot_api_key"] = session["api_key"]
            kwargs["api_base"] = session["api_base"]
        # anthropic provider: SDK reads ANTHROPIC_API_KEY / BASE_URL from
        # process env directly (these are read-only here, not mutated).
        return kwargs

    def _workspace_tools(self, session: TmuxSession) -> WorkspaceTools:
        commands_path = getattr(session, "_commands_path", None)
        return WorkspaceTools(
            workspace="/workspace",
            backend=ContainerWorkspaceBackend(session._container),
            commands_path=commands_path,
        )

    def _result_path(self, logging_dir: Path) -> Path:
        return logging_dir / self._RESULT_FILENAME

    def _trajectory_path(self, logging_dir: Path) -> Path:
        return logging_dir / "trajectories" / self._TRAJECTORY_FILENAME

    def _load_result_payload(self, path: Path) -> dict[str, object] | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
        timeout_sec: float | None = None,
    ) -> AgentResult:
        if logging_dir is None:
            raise ValueError("ClaudeCodeAgent requires logging_dir for result capture")

        trajectory_path = self._trajectory_path(logging_dir)
        result_path = self._result_path(logging_dir)
        trajectory_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.parent.mkdir(parents=True, exist_ok=True)

        # Default: effectively unlimited SDK turns; wall clock still capped by timeout_sec.
        _max_iter_raw = os.environ.get("LHB_CLAUDE_SDK_MAX_ITERATIONS", str(10**9))
        try:
            max_iterations = max(1, int(_max_iter_raw))
        except ValueError:
            max_iterations = 10**9

        run_task(
            instruction=instruction,
            workspace="/workspace",
            model=self._model_name,
            trajectory_path=trajectory_path,
            result_path=result_path,
            max_iterations=max_iterations,
            timeout_sec=timeout_sec,
            tools=self._workspace_tools(session),
            **self._sdk_kwargs(),
        )

        payload = self._load_result_payload(result_path)
        if payload is None:
            failure_mode = (
                "agent_startup_error"
                if not trajectory_path.exists() or trajectory_path.stat().st_size == 0
                else "unknown_agent_error"
            )
            return AgentResult(failure_mode=failure_mode)

        failure_mode = str(payload.get("failure_mode", "unknown_agent_error"))
        conversation_started = bool(payload.get("conversation_started", False))
        trajectory_missing = (
            not trajectory_path.exists() or trajectory_path.stat().st_size == 0
        )
        if trajectory_missing and not conversation_started and failure_mode == "unknown_agent_error":
            failure_mode = "agent_startup_error"

        return AgentResult(
            total_input_tokens=int(payload.get("total_input_tokens", 0)),
            total_output_tokens=int(payload.get("total_output_tokens", 0)),
            failure_mode=failure_mode,
        )
