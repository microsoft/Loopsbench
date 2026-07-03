"""Tmux session wrapper for interacting with containers."""

from __future__ import annotations

import shlex
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from long_horizon_bench.utils.logger import logger

if TYPE_CHECKING:
    from docker.models.containers import Container
else:
    Container = Any


class TmuxSession:
    """Manages a tmux session inside a Docker container.

    Provides methods to send commands, wait for output, and capture
    the terminal pane content.
    """

    _PROMPT_SENTINEL = "LHB_CMD_DONE"
    _POLL_INTERVAL = 0.5

    def __init__(
        self,
        container: Container,
        session_name: str,
        commands_path: Path | None = None,
        container_name: str | None = None,
    ):
        self._container = container
        self._session_name = session_name
        self._commands_path = commands_path
        self._container_name = container_name
        self._logger = logger.getChild(__name__)

        # Create the tmux session inside the container.
        # Use a very wide terminal so long commands (e.g. those that embed the
        # full task instruction ~1500 chars) never wrap.  If the command echo
        # wraps, the sentinel appended at the end can be split across two lines,
        # making pane.count(sentinel) == 1 instead of 2 and causing a timeout.
        # 10 000 columns is far wider than any command we send.
        self._exec(f"tmux new-session -d -s {self._session_name} -x 10000 -y 50")

    # -- low-level helpers --

    def _exec(self, cmd: str) -> str:
        """Execute a command in the container and return stdout."""
        exit_code, output = self._container.exec_run(
            ["bash", "-c", cmd], demux=True
        )
        stdout = (output[0] or b"").decode("utf-8", errors="replace")
        return stdout

    # -- public API --

    def _append_command_log(self, keys: str) -> None:
        if self._commands_path is None:
            return
        ts = datetime.now(timezone.utc).isoformat()
        with self._commands_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts}\t{keys}\n")

    def send_keys(
        self,
        keys: list[str] | str,
        block: bool = True,
        max_timeout_sec: float = 300.0,
    ) -> None:
        """Send keys to the tmux session.

        Args:
            keys: Keys to send (strings joined together).
            block: If True, wait until the command finishes (sentinel detected).
            max_timeout_sec: Maximum time to wait when blocking.
        """
        if isinstance(keys, list):
            key_str = " ".join(keys)
        else:
            key_str = keys

        self._append_command_log(key_str)

        # tmux treats each space-separated argument as a separate key name,
        # so "cd /app Enter" would type "cd/app" and press Enter (no space).
        # Fix: quote the command portion so tmux receives it as a single
        # literal string, while leaving recognized tmux key names (e.g.
        # "Enter") as separate unquoted arguments so they are interpreted
        # as key presses rather than typed text.
        _TMUX_KEY_NAMES = {
            "Enter", "Space", "Escape", "Tab", "BSpace",
            "Up", "Down", "Left", "Right",
            "F1", "F2", "F3", "F4", "F5", "F6",
            "F7", "F8", "F9", "F10", "F11", "F12",
        }
        parts = key_str.split()
        trailing: list[str] = []
        while parts and parts[-1] in _TMUX_KEY_NAMES:
            trailing.insert(0, parts.pop())

        sentinel: str | None = None
        sentinel_file: str | None = None
        if block and "Enter" in trailing:
            # Use a unique sentinel token + a marker file.  The marker file is
            # created by the shell after the command exits, so polling its
            # existence is a reliable completion signal that does NOT depend on
            # tmux scrollback history limits or terminal wrapping.
            sentinel = f"{self._PROMPT_SENTINEL}_{uuid.uuid4().hex[:12]}"
            sentinel_file = f"/tmp/lhb_{sentinel}"
            cmd_text = " ".join(parts)
            # '; echo SENTINEL' keeps the text visible in the pane for
            # debugging.  '; touch SENTINEL_FILE' is the actual reliable
            # completion flag that _wait_for_sentinel polls.
            # Both use ';' (not '&&') so they run even if cmd exits non-zero.
            # set -e inside subshells (e.g. run-tests.sh) does NOT propagate
            # to the outer tmux shell, so the touch always fires.
            cmd_with_sentinel = (
                f"{cmd_text}; echo {sentinel}; touch {sentinel_file}"
            )
            tmux_args: list[str] = [shlex.quote(cmd_with_sentinel)] + trailing
        else:
            tmux_args = []
            if parts:
                tmux_args.append(shlex.quote(" ".join(parts)))
            tmux_args.extend(trailing)

        self._exec(
            f"tmux send-keys -t {self._session_name} {' '.join(tmux_args)}"
        )

        if block:
            if sentinel is not None:
                assert sentinel_file is not None
                self._wait_for_sentinel(sentinel, sentinel_file, max_timeout_sec)
            else:
                # Non-Enter blocking (rare) — fall back to prompt heuristic
                self._wait_for_prompt(max_timeout_sec)

    def _wait_for_sentinel(
        self, sentinel: str, sentinel_file: str, timeout_sec: float
    ) -> None:
        """Poll for the sentinel marker file created after the command exits.

        Rather than scanning the tmux scrollback (which can silently drop the
        command-echo occurrence when output exceeds the history-limit), we
        instruct the shell to ``touch <sentinel_file>`` after the command and
        poll for that file's existence.  This approach is:

        * Immune to tmux scrollback history limits (no buffer scanning).
        * Immune to terminal-width wrapping of long commands.
        * Immune to ANSI escape sequences in command output.
        * Correct when commands emit thousands of output lines (SWE-agent,
          OpenHands installation output, etc.).
        """
        time.sleep(self._POLL_INTERVAL)
        start = time.time()
        while time.time() - start < timeout_sec:
            exit_code, _ = self._container.exec_run(
                ["bash", "-c", f"test -f {sentinel_file}"],
                demux=True,
            )
            if exit_code == 0:
                return
            time.sleep(self._POLL_INTERVAL)
        raise TimeoutError(
            f"Tmux session '{self._session_name}' sentinel '{sentinel}' not "
            f"found within {timeout_sec}s"
        )

    def _wait_for_prompt(self, timeout_sec: float) -> None:
        """Fallback: poll until last line ends with $ or # (prompt heuristic)."""
        time.sleep(self._POLL_INTERVAL)
        start = time.time()
        while time.time() - start < timeout_sec:
            pane = self.capture_pane()
            lines = pane.strip().splitlines()
            if lines:
                last_line = lines[-1].strip()
                if last_line.endswith("$") or last_line.endswith("#"):
                    return
            time.sleep(self._POLL_INTERVAL)
        raise TimeoutError(
            f"Tmux session '{self._session_name}' did not return to prompt "
            f"within {timeout_sec}s"
        )

    def capture_pane(self, capture_entire: bool = False) -> str:
        """Capture the current pane content.

        Args:
            capture_entire: If True, capture the full scrollback history.
        """
        if capture_entire:
            # Capture full scrollback into the tmux buffer, then read it.
            self._exec(f"tmux capture-pane -t {self._session_name} -S -")
            return self._exec(f"tmux show-buffer")
        else:
            # Capture only the visible pane directly to stdout.
            return self._exec(
                f"tmux capture-pane -t {self._session_name} -p"
            )
