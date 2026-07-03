"""Cursor headless agent integration for Long-Horizon-Bench."""

import io
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

from long_horizon_bench.agents.agent_name import AgentName
from long_horizon_bench.agents.base_agent import AgentResult, BaseAgent
from long_horizon_bench.terminal.tmux_session import TmuxSession

# Cursor's own model identifiers, listed by `agent models`.
# Maps the harness's anthropic/<name> shorthand to cursor's model ID.
_ANTHROPIC_TO_CURSOR: dict[str, str] = {
    "claude-sonnet-4-6": "claude-4.6-sonnet-medium",
    "claude-opus-4-6": "claude-4.6-opus-high",
    "claude-sonnet-4-5": "claude-4.5-sonnet",
    "claude-opus-4-5": "claude-4.5-opus-high",
    "claude-haiku-4-5": "claude-4.5-sonnet",
    "claude-sonnet-4": "claude-4-sonnet",
}

# Host-side paths for cursor authentication and binary.
_CURSOR_AUTH_JSON = Path.home() / ".config" / "cursor" / "auth.json"
_CURSOR_AGENT_BIN = Path.home() / ".local" / "bin" / "agent"


class CursorAgent(BaseAgent):
    """Run tasks using the Cursor headless agent binary on the host machine.

    Rather than installing cursor-agent inside every fresh Docker container
    (which takes 5+ minutes), this agent runs the host's existing cursor-agent
    installation directly.  The task workspace is extracted from the container
    to a host-side temporary directory, cursor-agent operates on it via the
    ``--workspace`` flag, and the modified files are copied back into the
    container before the test runner executes.

    Authentication — two supported approaches, in priority order:

    1. **CURSOR_API_KEY** env var: a Cursor Cloud Agent key obtained from
       https://cursor.com/dashboard?tab=cloud-agents.
    2. **Stored session tokens**: run ``agent login`` once on the host machine.
       Tokens are saved to ``~/.config/cursor/auth.json`` and used automatically.

    Model names use Cursor's own identifiers or any model cursor proxies to:

    - ``cursor/claude-4.6-sonnet-medium`` — pass the cursor model name directly.
    - ``cursor/composer-2`` — Cursor's proprietary model.
    - ``anthropic/claude-sonnet-4-6`` — automatically mapped to
      ``claude-4.6-sonnet-medium``.
    - ``openai/gpt-4o`` — passed through directly to cursor's proxy.
    """

    def __init__(self, model_name: str, **kwargs):
        super().__init__(**kwargs)
        self._model_name = model_name
        # When ANTHROPIC_API_KEY is set, use the provider's own API directly
        # (via ANTHROPIC_BASE_URL proxy if set) instead of Cursor cloud.
        if model_name.startswith("cursor/"):
            # Strip the "cursor/" prefix; the remainder is cursor's native name.
            self._model = model_name.split("/", 1)[1]
        elif "/" in model_name:
            provider, bare = model_name.split("/", 1)
            if provider == "anthropic":
                self._model = _ANTHROPIC_TO_CURSOR.get(bare, bare)
            else:
                self._model = bare
        else:
            self._model = model_name
        self._validate_api_key()

    @staticmethod
    def name() -> str:
        return AgentName.CURSOR.value

    def _validate_api_key(self) -> None:
        """Raise ValueError early if no authentication will be available."""
        if "CURSOR_API_KEY" in os.environ:
            return
        if "ANTHROPIC_API_KEY" in os.environ:
            return
        if _CURSOR_AUTH_JSON.exists():
            return
        raise ValueError(
            "No authentication found for CursorAgent.  Options:\n"
            "  1. Set ANTHROPIC_API_KEY (+ optionally ANTHROPIC_BASE_URL) to use "
            "Anthropic API directly, OR\n"
            "  2. Set CURSOR_API_KEY (obtain from "
            "https://cursor.com/dashboard?tab=cloud-agents), OR\n"
            "  3. Run `agent login` on this host to store session tokens "
            f"in {_CURSOR_AUTH_JSON}."
        )

    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
        timeout_sec: float | None = None,
    ) -> AgentResult:
        container = session._container

        # Determine the container's working directory (task workspace).
        workdir = (
            container.attrs.get("Config", {}).get("WorkingDir") or "/workspace"
        )

        with tempfile.TemporaryDirectory(prefix="lhb-cursor-") as tmp_str:
            tmp = Path(tmp_str)

            # Extract workspace from container to a host-side temp directory.
            # get_archive('/workspace') yields a tar whose entries are named
            # 'workspace/...', so extractall(tmp) creates tmp/workspace/.
            bits, _ = container.get_archive(workdir)
            with tarfile.open(fileobj=io.BytesIO(b"".join(bits))) as tar:
                tar.extractall(tmp)
            local_ws = tmp / Path(workdir).name

            # Run cursor-agent on the host, operating on the local workspace.
            env = os.environ.copy()
            env["HOME"] = str(Path.home())
            cmd = [
                str(_CURSOR_AGENT_BIN),
                "-p", "--trust", "--force",
                "--model", self._model,
                "--workspace", str(local_ws),
                instruction,
            ]
            if logging_dir is not None:
                logging_dir.mkdir(parents=True, exist_ok=True)
                log_path = logging_dir / "cursor.log"
                with open(log_path, "w") as lf:
                    subprocess.run(
                        cmd, env=env, timeout=86400, stdout=lf, stderr=lf
                    )
            else:
                subprocess.run(cmd, env=env, timeout=86400)

            # Copy modified workspace back into the container.
            # arcname="." makes tar entries relative to workdir root so that
            # put_archive(workdir, ...) places files directly under /workspace/.
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                tar.add(local_ws, arcname=".")
            buf.seek(0)
            container.put_archive(workdir, buf.read())

        return AgentResult()
