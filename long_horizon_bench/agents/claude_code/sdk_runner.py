"""Local Claude SDK runner used by the benchmark harness."""

from __future__ import annotations

import argparse
import io
import json
import os
import posixpath
import shlex
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

SYSTEM_PROMPT = (
    "You are Claude running inside a benchmark task container. "
    "Use the provided tools to inspect files, edit the workspace, and run validation. "
    "Keep progress updates short and finish only when your workspace changes are ready for the harness tests."
)

TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command in the task workspace and return stdout, stderr, and exit code.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute in the workspace.",
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": "Maximum runtime in seconds.",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file from the workspace with numbered lines.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative or absolute path inside the workspace.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Zero-based starting line offset.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to read.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_file",
        "description": "Write a UTF-8 text file in the workspace, creating parent directories if needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative or absolute path inside the workspace.",
                },
                "content": {
                    "type": "string",
                    "description": "Complete file contents to write.",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "replace_in_file",
        "description": "Replace an exact string in a UTF-8 text file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative or absolute path inside the workspace.",
                },
                "old_string": {
                    "type": "string",
                    "description": "Exact text to replace.",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text.",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Whether to replace every occurrence.",
                },
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        },
    },
]


class LocalWorkspaceBackend:
    def bash(self, workspace: str, command: str, timeout_sec: int) -> tuple[int, str, str]:
        timeout = max(1, min(int(timeout_sec), 600))
        completed = subprocess.run(
            command,
            shell=True,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return completed.returncode, completed.stdout, completed.stderr

    def read_text(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def write_text(self, path: str, content: str) -> None:
        resolved = Path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")


class ContainerWorkspaceBackend:
    def __init__(self, container: Any):
        self._container = container

    def bash(self, workspace: str, command: str, timeout_sec: int) -> tuple[int, str, str]:
        timeout = max(1, min(int(timeout_sec), 600))
        wrapped_command = (
            "if command -v timeout >/dev/null 2>&1; then "
            f"timeout {timeout} bash -lc {shlex.quote(command)}; "
            "else "
            f"bash -lc {shlex.quote(command)}; "
            "fi"
        )
        exit_code, output = self._container.exec_run(
            ["bash", "-lc", wrapped_command],
            workdir=workspace,
            demux=True,
        )
        stdout = (output[0] or b"").decode("utf-8", errors="replace")
        stderr = (output[1] or b"").decode("utf-8", errors="replace")
        return exit_code, stdout, stderr

    def read_text(self, path: str) -> str:
        bits, _ = self._container.get_archive(path)
        archive = io.BytesIO(b"".join(bits))
        with tarfile.open(fileobj=archive) as tar:
            for member in tar:
                if member.isfile():
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        break
                    return extracted.read().decode("utf-8", errors="strict")
        raise FileNotFoundError(path)

    def write_text(self, path: str, content: str) -> None:
        target = PurePosixPath(path)
        parent = str(target.parent)
        self._container.exec_run(
            ["bash", "-lc", f"mkdir -p {shlex.quote(parent)}"],
            demux=True,
        )

        payload = content.encode("utf-8")
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w") as tar:
            info = tarfile.TarInfo(name=target.name)
            info.size = len(payload)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(payload))
        tar_buffer.seek(0)
        self._container.put_archive(parent, tar_buffer.getvalue())


class WorkspaceTools:
    def __init__(
        self,
        workspace: Path | str,
        backend: Any | None = None,
        commands_path: Path | None = None,
    ):
        workspace_text = workspace.as_posix() if isinstance(workspace, Path) else str(workspace)
        if not workspace_text.startswith("/"):
            workspace_text = Path(workspace_text).resolve().as_posix()
        self.workspace = PurePosixPath(posixpath.normpath(workspace_text))
        self._backend = backend or LocalWorkspaceBackend()
        self._commands_path = commands_path

    def _resolve_path(self, raw_path: str) -> PurePosixPath:
        path = PurePosixPath(raw_path)
        candidate = path if path.is_absolute() else self.workspace / path
        normalized = PurePosixPath(posixpath.normpath(candidate.as_posix()))
        normalized.relative_to(self.workspace)
        return normalized

    def _record_command(self, command: str, exit_code: int) -> None:
        if self._commands_path is None:
            return
        try:
            self._commands_path.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).isoformat()
            line = f"{ts}\t{exit_code}\t{command}\n"
            with self._commands_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass

    def bash(self, command: str, timeout_sec: int = 120) -> str:
        exit_code, stdout, stderr = self._backend.bash(
            self.workspace.as_posix(),
            command,
            timeout_sec,
        )
        self._record_command(command, exit_code)
        payload = {
            "exit_code": exit_code,
            "stdout": stdout[-12000:],
            "stderr": stderr[-12000:],
        }
        return json.dumps(payload, ensure_ascii=False)

    def read_file(self, path: str, offset: int = 0, limit: int = 200) -> str:
        resolved = self._resolve_path(path)
        lines = self._backend.read_text(resolved.as_posix()).splitlines()
        start = max(0, int(offset))
        end = start + max(1, min(int(limit), 1000))
        numbered = [f"{idx + 1}\t{line}" for idx, line in enumerate(lines[start:end], start=start)]
        return "\n".join(numbered)

    def write_file(self, path: str, content: str) -> str:
        resolved = self._resolve_path(path)
        self._backend.write_text(resolved.as_posix(), content)
        return f"Wrote {resolved.relative_to(self.workspace)} ({len(content)} chars)."

    def replace_in_file(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        resolved = self._resolve_path(path)
        original = self._backend.read_text(resolved.as_posix())
        count = original.count(old_string)
        if count == 0:
            raise ValueError("old_string not found")
        if not replace_all and count != 1:
            raise ValueError("old_string is not unique; use replace_all=true")
        updated = (
            original.replace(old_string, new_string)
            if replace_all
            else original.replace(old_string, new_string, 1)
        )
        self._backend.write_text(resolved.as_posix(), updated)
        replaced = count if replace_all else 1
        return f"Updated {resolved.relative_to(self.workspace)} with {replaced} replacement(s)."

    def execute(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        if tool_name == "bash":
            return self.bash(**tool_input)
        if tool_name == "read_file":
            return self.read_file(**tool_input)
        if tool_name == "write_file":
            return self.write_file(**tool_input)
        if tool_name == "replace_in_file":
            return self.replace_in_file(**tool_input)
        raise ValueError(f"Unknown tool: {tool_name}")


class TrajectoryWriter:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, payload: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _request_options(model: str) -> dict[str, Any]:
    options: dict[str, Any] = {
        "model": model,
        "max_tokens": 16000,
        "system": SYSTEM_PROMPT,
        "messages": [],
        "tools": TOOLS,
    }
    if model in {"claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6"}:
        options["thinking"] = {"type": "adaptive"}
        options["output_config"] = {"effort": "high"}
    return options


def _openai_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
        for tool in TOOLS
    ]


def _azure_client(azure_ad_token_provider=None, api_base=None, api_version=None):
    """Create an AzureOpenAI client.

    Pass `azure_ad_token_provider` (a zero-arg callable returning a fresh
    token) so the SDK can re-authenticate when the cached AAD token expires
    on long trials. The previous implementation captured a single token in a
    closure (`lambda: token`) which silently expired ~1h in and turned every
    subsequent request into a 401, killing long trials.
    """
    from openai import AzureOpenAI

    api_base = api_base or os.environ.get("AZURE_API_BASE", "https://cloudgpt-openai.azure-api.net")
    api_version = api_version or os.environ.get("AZURE_API_VERSION", "2025-04-01-preview")
    if azure_ad_token_provider is None:
        token = os.environ["AZURE_OPENAI_AD_TOKEN"]
        azure_ad_token_provider = lambda: token  # noqa: E731 — back-compat fallback
    return AzureOpenAI(
        azure_endpoint=api_base,
        api_version=api_version,
        azure_ad_token_provider=azure_ad_token_provider,
    )


def _copilot_client(api_key=None, api_base=None):
    import httpx
    from openai import OpenAI

    api_base = api_base or os.environ.get("GITHUB_COPILOT_API_BASE", "https://api.githubcopilot.com")
    api_key = api_key or os.environ["GITHUB_COPILOT_API_KEY"]
    default_headers = {
        "Copilot-Integration-Id": os.environ.get(
            "GITHUB_COPILOT_INTEGRATION_ID", "vscode-chat"
        ),
        "Editor-Version": os.environ.get("GITHUB_COPILOT_EDITOR_VERSION", "vscode/1.100.0"),
        "Editor-Plugin-Version": os.environ.get(
            "GITHUB_COPILOT_EDITOR_PLUGIN_VERSION", "copilot-chat/0.38.0"
        ),
        "Openai-Intent": os.environ.get(
            "GITHUB_COPILOT_OPENAI_INTENT", "conversation-edits"
        ),
        "User-Agent": os.environ.get("GITHUB_COPILOT_USER_AGENT", "long-horizon-bench/0.1.0"),
        "X-GitHub-Api-Version": os.environ.get(
            "GITHUB_COPILOT_API_VERSION", "2025-10-01"
        ),
    }
    beta_header = os.environ.get("GITHUB_COPILOT_ANTHROPIC_BETA")
    if beta_header:
        default_headers["anthropic-beta"] = beta_header
    http_client = httpx.Client(headers=default_headers)
    return OpenAI(api_key=api_key, base_url=api_base.rstrip("/"), http_client=http_client)


def _classify_failure(exc: Exception) -> str:
    """Map an SDK exception class to a coarse failure_mode bucket.

    Distinguishing auth / rate-limit / network from generic logic errors lets
    the post-run report show "this trial died because of infra", not "unknown".
    """
    name = type(exc).__name__
    if name in ("AuthenticationError", "PermissionDeniedError"):
        return "agent_auth_error"
    if name == "RateLimitError":
        return "agent_rate_limited"
    if name in ("APIConnectionError", "APITimeoutError"):
        return "agent_network_error"
    if name == "APIStatusError":
        status = getattr(exc, "status_code", None)
        if status == 401:
            return "agent_auth_error"
        if status == 429:
            return "agent_rate_limited"
        if status and status >= 500:
            return "agent_server_error"
    return "unknown_agent_error"


def _call_with_retry(make_request, *, max_attempts: int = 4, base_delay: float = 2.0):
    """Invoke `make_request()` with retry on transient SDK errors.

    Retries on 429 / 5xx / network errors with exponential backoff. Lets 401
    through one retry too — the AzureOpenAI client refreshes its AAD token via
    the provider callable on the next call, so a stale-token 401 typically
    resolves immediately. Anything else (validation errors, 4xx other than 401)
    propagates so the caller can record an honest failure_mode.
    """
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return make_request()
        except Exception as exc:  # noqa: BLE001 — classify by type below
            mode = _classify_failure(exc)
            transient = mode in (
                "agent_rate_limited",
                "agent_network_error",
                "agent_server_error",
                "agent_auth_error",
            )
            if not transient or attempt == max_attempts - 1:
                raise
            if mode == "agent_auth_error":
                time.sleep(0.5)
            else:
                time.sleep(base_delay * (2 ** attempt))
            last_exc = exc
    if last_exc is not None:  # pragma: no cover
        raise last_exc


def _write_result(
    result_path: Path,
    *,
    total_input_tokens: int,
    total_output_tokens: int,
    failure_mode: str,
    final_text: str,
    conversation_started: bool,
) -> None:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(
            {
                "total_input_tokens": total_input_tokens,
                "total_output_tokens": total_output_tokens,
                "failure_mode": failure_mode,
                "final_text": final_text,
                "conversation_started": conversation_started,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _run_openai_chat_loop(
    *,
    client: Any,
    instruction: str,
    model_name: str,
    trace: TrajectoryWriter,
    tools: WorkspaceTools,
    max_iterations: int,
    deadline: float | None = None,
) -> tuple[int, int, str, bool, str]:
    total_input_tokens = 0
    total_output_tokens = 0
    failure_mode = "none"
    final_text = ""
    conversation_started = False
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": instruction},
    ]
    for iteration in range(max_iterations):
        if deadline is not None and time.monotonic() >= deadline:
            failure_mode = "agent_timeout"
            break
        response = _call_with_retry(
            lambda: client.chat.completions.create(
                model=model_name,
                messages=messages,
                tools=_openai_tools(),
                tool_choice="auto",
            )
        )
        conversation_started = True
        message = response.choices[0].message
        total_input_tokens += getattr(response.usage, "prompt_tokens", 0) or 0
        total_output_tokens += getattr(response.usage, "completion_tokens", 0) or 0
        trace.write(
            {
                "event": "response",
                "iteration": iteration,
                "message": response.model_dump(),
                "finish_reason": response.choices[0].finish_reason,
            }
        )

        if message.content:
            final_text = message.content

        assistant_message: dict[str, Any] = {"role": "assistant"}
        if message.content is not None:
            assistant_message["content"] = message.content
        tool_calls = getattr(message, "tool_calls", None) or []
        if tool_calls:
            assistant_message["tool_calls"] = [tc.model_dump() for tc in tool_calls]
        messages.append(assistant_message)

        if not tool_calls:
            break

        for tool_call in tool_calls:
            try:
                tool_input = json.loads(tool_call.function.arguments)
                result = tools.execute(tool_call.function.name, tool_input)
                trace.write(
                    {
                        "event": "tool_result",
                        "iteration": iteration,
                        "tool_name": tool_call.function.name,
                        "tool_use_id": tool_call.id,
                        "input": tool_input,
                        "result": result,
                        "is_error": False,
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    }
                )
            except Exception as exc:
                error_text = f"Error: {exc}"
                trace.write(
                    {
                        "event": "tool_result",
                        "iteration": iteration,
                        "tool_name": tool_call.function.name,
                        "tool_use_id": tool_call.id,
                        "input": tool_call.function.arguments,
                        "result": error_text,
                        "is_error": True,
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": error_text,
                    }
                )
    else:
        failure_mode = "agent_timeout"

    return (
        total_input_tokens,
        total_output_tokens,
        final_text,
        conversation_started,
        failure_mode,
    )


def run_task(
    instruction: str,
    workspace: Path | str,
    model: str,
    trajectory_path: Path,
    result_path: Path,
    max_iterations: int,
    timeout_sec: float | None = None,
    tools: WorkspaceTools | None = None,
    azure_ad_token_provider=None,
    copilot_api_key: str | None = None,
    api_base: str | None = None,
    api_version: str | None = None,
) -> int:
    """Run an agent loop end-to-end and write trajectory + result files.

    `azure_ad_token_provider` and `copilot_api_key` may be passed explicitly so
    callers can avoid mutating the global ``os.environ`` (a race in
    ThreadPoolExecutor-based concurrent runs). When omitted, fall back to the
    legacy behavior of reading from environment variables.
    """
    provider, model_name = model.split("/", 1) if "/" in model else ("anthropic", model)
    tools = tools or WorkspaceTools(workspace)
    trace = TrajectoryWriter(trajectory_path)
    total_input_tokens = 0
    total_output_tokens = 0
    failure_mode = "none"
    final_text = ""
    conversation_started = False
    deadline = None if timeout_sec is None else time.monotonic() + timeout_sec

    trace.write(
        {
            "event": "start",
            "model": model,
            "workspace": str(workspace),
            "instruction": instruction,
        }
    )

    try:
        if provider == "azure":
            client = _azure_client(
                azure_ad_token_provider=azure_ad_token_provider,
                api_base=api_base,
                api_version=api_version,
            )
            (
                total_input_tokens,
                total_output_tokens,
                final_text,
                conversation_started,
                failure_mode,
            ) = _run_openai_chat_loop(
                client=client,
                instruction=instruction,
                model_name=model_name,
                trace=trace,
                tools=tools,
                max_iterations=max_iterations,
                deadline=deadline,
            )
        elif provider == "copilot":
            client = _copilot_client(api_key=copilot_api_key, api_base=api_base)
            (
                total_input_tokens,
                total_output_tokens,
                final_text,
                conversation_started,
                failure_mode,
            ) = _run_openai_chat_loop(
                client=client,
                instruction=instruction,
                model_name=model_name,
                trace=trace,
                tools=tools,
                max_iterations=max_iterations,
                deadline=deadline,
            )
        elif provider == "anthropic":
            import anthropic  # type: ignore

            client = anthropic.Anthropic()
            messages = [{"role": "user", "content": instruction}]
            for iteration in range(max_iterations):
                if deadline is not None and time.monotonic() >= deadline:
                    failure_mode = "agent_timeout"
                    break
                request = _request_options(model_name)
                request["messages"] = messages
                response = _call_with_retry(lambda: client.messages.create(**request))
                conversation_started = True
                total_input_tokens += getattr(response.usage, "input_tokens", 0)
                total_output_tokens += getattr(response.usage, "output_tokens", 0)
                trace.write(
                    {
                        "event": "response",
                        "iteration": iteration,
                        "request_id": response._request_id,
                        "stop_reason": response.stop_reason,
                        "message": response.to_dict(),
                    }
                )

                text_blocks = [block.text for block in response.content if block.type == "text"]
                if text_blocks:
                    final_text = "\n".join(text_blocks)

                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason == "end_turn":
                    break

                if response.stop_reason == "pause_turn":
                    continue

                tool_use_blocks = [block for block in response.content if block.type == "tool_use"]
                if not tool_use_blocks:
                    break

                tool_results: list[dict[str, Any]] = []
                for block in tool_use_blocks:
                    try:
                        result = tools.execute(block.name, dict(block.input))
                        trace.write(
                            {
                                "event": "tool_result",
                                "iteration": iteration,
                                "tool_name": block.name,
                                "tool_use_id": block.id,
                                "input": dict(block.input),
                                "result": result,
                                "is_error": False,
                            }
                        )
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result,
                            }
                        )
                    except Exception as exc:
                        error_text = f"Error: {exc}"
                        trace.write(
                            {
                                "event": "tool_result",
                                "iteration": iteration,
                                "tool_name": block.name,
                                "tool_use_id": block.id,
                                "input": dict(block.input),
                                "result": error_text,
                                "is_error": True,
                            }
                        )
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": error_text,
                                "is_error": True,
                            }
                        )

                messages.append({"role": "user", "content": tool_results})
            else:
                failure_mode = "agent_timeout"
        else:
            raise ValueError(f"Unsupported provider: {provider}")
    except Exception as exc:
        classified = _classify_failure(exc)
        if classified != "unknown_agent_error":
            failure_mode = classified
        elif failure_mode == "none":
            failure_mode = "agent_startup_error" if not conversation_started else "unknown_agent_error"
        trace.write(
            {
                "event": "error",
                "phase": "startup" if not conversation_started else "run",
                "error": str(exc),
                "error_type": type(exc).__name__,
                "failure_mode": failure_mode,
            }
        )
    finally:
        _write_result(
            result_path,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            failure_mode=failure_mode,
            final_text=final_text,
            conversation_started=conversation_started,
        )

    return 0 if failure_mode == "none" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instruction-file", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--trajectory", required=True)
    parser.add_argument("--result", required=True)
    _default_max = int(os.environ.get("LHB_CLAUDE_SDK_MAX_ITERATIONS", str(10**9)))
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=max(1, _default_max),
        help="Anthropic/OpenAI tool-call iterations per trial (default from "
        "LHB_CLAUDE_SDK_MAX_ITERATIONS or 1e9).",
    )
    parser.add_argument("--timeout-sec", type=float)
    args = parser.parse_args()

    instruction = Path(args.instruction_file).read_text(encoding="utf-8")
    workspace = Path(args.workspace)
    return run_task(
        instruction=instruction,
        workspace=workspace,
        model=args.model,
        trajectory_path=Path(args.trajectory),
        result_path=Path(args.result),
        max_iterations=max(1, args.max_iterations),
        timeout_sec=args.timeout_sec,
    )


if __name__ == "__main__":
    sys.exit(main())
