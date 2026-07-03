"""Core execution engine for Long-Horizon-Bench."""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

from long_horizon_bench.agents.agent_factory import AgentFactory
from long_horizon_bench.agents.agent_name import AgentName
from long_horizon_bench.agents.base_agent import AgentResult, BaseAgent
from long_horizon_bench.config import config as lhb_config
from long_horizon_bench.dataset.dataset import Dataset
from long_horizon_bench.handlers.trial_handler import TrialHandler
from long_horizon_bench.harness.instruction_composer import compose_instruction
from long_horizon_bench.harness.models import (
    BenchmarkResults,
    FailureMode,
    ParsedResultSet,
    RegressionResults,
    ResultArtifact,
    RunMetadata,
    TrialResults,
    build_run_metadata,
    write_task_image_resolution,
)
from long_horizon_bench.parsers._pytest_tail_session import tail_case_id
from long_horizon_bench.parsers.base_parser import ParseContext, UnitTestStatus
from long_horizon_bench.parsers.parser_factory import ParserFactory, ResultFormat
from long_horizon_bench.utils.logger import logger
from long_horizon_bench.terminal.docker_compose_manager import (
    DockerComposeManager,
    docker_command,
    docker_env,
)
from long_horizon_bench.terminal.terminal import (
    Terminal,
    TerminalStartupError,
    spin_up_terminal,
)
from long_horizon_bench.harness.regression_harness import (
    HarnessConfig as RegressionHarnessConfig,
    RegressionHarness,
)
from long_horizon_bench.task_images.strategy import (
    DockerImageStrategy,
    resolve_task_docker_image,
    validate_remote_docker_image_coordinates,
)

# Merged with each task compose so the tester mounts host `tests/` + `run-tests.sh`.
_REGRESSION_COMPOSE_PATH = Path(__file__).resolve().parent / "regression.compose.yaml"

REGRESSION_POLL_INTERVAL_SEC = 15.0


class Harness:
    """Runs tasks, agents, and tests, then collects results."""

    def __init__(
        self,
        output_path: Path,
        run_id: str,
        agent_name: AgentName | None = None,
        agent_import_path: str | None = None,
        dataset_name: str | None = None,
        dataset_version: str | None = None,
        dataset_path: Path | None = None,
        model_name: str | None = None,
        agent_kwargs: dict[str, Any] | None = None,
        no_rebuild: bool = False,
        docker_image_strategy: DockerImageStrategy | str = DockerImageStrategy.REMOTE,
        docker_image_namespace: str | None = None,
        docker_image_tag: str | None = None,
        cleanup: bool = False,
        log_level: int = logging.INFO,
        task_ids: list[str] | None = None,
        n_tasks: int | None = None,
        livestream: bool = False,
        n_concurrent_trials: int = 4,
        exclude_task_ids: list[str] | None = None,
        n_attempts: int = 1,
        global_timeout_multiplier: float = 1.0,
        global_agent_timeout_sec: float | None = None,
        global_test_timeout_sec: float | None = None,
    ):
        self._start_time = datetime.now(timezone.utc).isoformat()

        self._dataset_path = dataset_path
        self._dataset_name = dataset_name
        self._dataset_version = dataset_version
        self._n_tasks = n_tasks
        self._task_ids = task_ids
        self._exclude_task_ids = exclude_task_ids

        self._output_path = output_path
        self._agent_name = agent_name
        self._agent_import_path = agent_import_path
        self._model_name = model_name if agent_name != AgentName.ORACLE else "Oracle"
        self._agent_kwargs = agent_kwargs or {}
        self._run_id = run_id
        self._no_rebuild = no_rebuild
        self._docker_image_strategy = DockerImageStrategy(docker_image_strategy)
        self._docker_image_namespace = docker_image_namespace
        self._docker_image_tag = docker_image_tag
        self._cleanup = cleanup
        self._log_level = log_level
        self._n_concurrent_trials = n_concurrent_trials
        self._n_attempts = n_attempts
        self._global_timeout_multiplier = global_timeout_multiplier
        self._global_agent_timeout_sec = global_agent_timeout_sec
        self._global_test_timeout_sec = global_test_timeout_sec
        validate_remote_docker_image_coordinates(
            strategy=self._docker_image_strategy,
            docker_image_namespace=self._docker_image_namespace,
            docker_image_tag=self._docker_image_tag,
        )

        # Rate-limit handling: in-round backoff retries + outer-loop continuation
        # (see _run_agent_with_rate_limit_retries / _is_retryable_agent_failure).
        rl_retries = self._agent_kwargs.get("rate_limit_retries_per_round")
        if rl_retries is None:
            rl_retries = int(os.environ.get("LHB_AGENT_RATE_LIMIT_RETRIES_PER_ROUND", "5"))
        rl_backoff = self._agent_kwargs.get("rate_limit_backoff_sec")
        if rl_backoff is None:
            rl_backoff = float(os.environ.get("LHB_AGENT_RATE_LIMIT_BACKOFF_SEC", "45"))
        self._rate_limit_retries_per_round = max(0, int(rl_retries))
        self._rate_limit_backoff_sec = max(0.0, float(rl_backoff))

        # Check if resuming
        self._is_resuming = self._run_path.exists()

        if not self._is_resuming:
            self._run_path.mkdir(parents=True, exist_ok=True)

        self._init_dataset()
        self._init_agent_class()

        self._livestream = livestream and (
            self._n_concurrent_trials == 1 or len(self._dataset) == 1
        )

        self._init_logger()
        self._dataset.sort_by_duration()

        if self._is_resuming:
            self._filter_completed_tasks()

    # -- path helpers --

    @property
    def _run_path(self) -> Path:
        return self._output_path / self._run_id

    @property
    def _results_output_path(self) -> Path:
        return self._run_path / "results.json"

    @property
    def _run_metadata_output_path(self) -> Path:
        return self._run_path / "run_metadata.json"

    @property
    def _log_output_path(self) -> Path:
        return self._run_path / "run.log"

    # -- init helpers --

    def _init_dataset(self) -> None:
        self._dataset = Dataset(
            name=self._dataset_name,
            version=self._dataset_version,
            path=self._dataset_path,
            task_ids=self._task_ids,
            n_tasks=self._n_tasks,
            exclude_task_ids=self._exclude_task_ids,
        )

    def _init_agent_class(self) -> None:
        self._agent_class = AgentFactory.get_agent_class(
            agent_name=self._agent_name,
            import_path=self._agent_import_path,
        )

    def _create_agent_for_task(self, task_id: str) -> BaseAgent:
        kwargs = self._agent_kwargs.copy()
        if self._agent_name == AgentName.ORACLE:
            kwargs["dataset_path"] = (
                self._dataset._path.absolute()
                if hasattr(self._dataset, "_path")
                else self._dataset_path
            )
            kwargs["task_ids"] = [task_id]
        return AgentFactory.get_agent(
            agent_name=self._agent_name,
            import_path=self._agent_import_path,
            **kwargs,
        )

    def _init_logger(self) -> None:
        mode = "a" if self._is_resuming else "w"
        file_handler = logging.FileHandler(self._log_output_path, mode=mode)
        file_handler.setLevel(logging.DEBUG)

        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        if not self._livestream:
            console = logging.StreamHandler()
            console.setLevel(self._log_level)
            console.setFormatter(formatter)
            logger.addHandler(console)

        self._logger = logger.getChild("harness")

    # -- resume --

    def _filter_completed_tasks(self) -> None:
        """Remove already-completed tasks when resuming."""
        completed: set[str] = set()
        incomplete: list[Path] = []
        previous_results = self._load_previous_results_list()

        for task_path in self._dataset._tasks:
            task_id = task_path.name
            all_done = True

            existing_trial_names = {
                result.trial_name
                for result in previous_results
                if result.task_id == task_id
            }
            for attempt in range(1, self._n_attempts + 1):
                trial_name = self._get_trial_name(task_path, attempt)
                if trial_name not in existing_trial_names:
                    all_done = False
                    break

            if all_done:
                completed.add(task_id)
            else:
                incomplete.append(task_path)
                # Clean up partial artifacts
                task_dir = self._run_path / task_id
                if task_dir.exists():
                    shutil.rmtree(task_dir, ignore_errors=True)

        self._dataset._tasks = incomplete

        if completed:
            self._logger.info(
                f"Resuming run {self._run_id}. "
                f"Skipping {len(completed)} completed tasks."
            )
        if not incomplete:
            self._logger.info("All tasks already completed.")

    # -- test execution --

    def _patch_timeline_path(self, handler: TrialHandler) -> Path:
        return handler.trial_paths.task_output_path / "patch_timeline.jsonl"

    def _record_patch_timeline_snapshot(
        self, handler: TrialHandler, terminal: Terminal
    ) -> None:
        container = terminal._container
        script = """
python3 - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

root = Path('/workspace/requirement_patches')
rows = []
if root.exists():
    for path in sorted(root.glob('*.diff')):
        stat = path.stat()
        rows.append({
            'requirement_slug': path.stem,
            'path': str(path),
            'size_bytes': stat.st_size,
            'mtime': datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })
print(json.dumps({
    'ts': datetime.now(timezone.utc).isoformat(),
    'files': rows,
}))
PY
""".strip()
        try:
            exit_code, output = container.exec_run(["sh", "-lc", script])
            if exit_code != 0:
                self._logger.debug(
                    f"{handler.task_id}: patch timeline snapshot failed with exit {exit_code}"
                )
                return
            payload = (output or b"").decode(errors="replace").strip()
            if not payload:
                return
            with self._patch_timeline_path(handler).open("a", encoding="utf-8") as fh:
                fh.write(payload + "\n")
        except Exception as exc:
            self._logger.debug(
                f"{handler.task_id}: patch timeline snapshot failed: {exc}"
            )

    def _start_patch_timeline_recorder(
        self, handler: TrialHandler, terminal: Terminal
    ) -> tuple[threading.Thread, threading.Event]:
        stop_event = threading.Event()

        def _loop() -> None:
            while not stop_event.is_set():
                self._record_patch_timeline_snapshot(handler, terminal)
                stop_event.wait(REGRESSION_POLL_INTERVAL_SEC)

        thread = threading.Thread(
            target=_loop,
            name=f"patch-timeline-{handler.task_id}",
            daemon=True,
        )
        thread.start()
        return thread, stop_event

    def _stop_patch_timeline_recorder(
        self,
        thread: threading.Thread | None,
        stop_event: threading.Event | None,
        handler: TrialHandler,
        terminal: Terminal,
    ) -> None:
        if thread is None or stop_event is None:
            return
        stop_event.set()
        thread.join(timeout=5.0)
        self._record_patch_timeline_snapshot(handler, terminal)

    # -- agent-visible workspace git --------------------------------------

    GIT_WORKFLOW_PROMPT = (
        "\n\n## Git workflow (required)\n\n"
        "`/workspace` is initialized as a git repo at the start of round 1. After\n"
        "finishing the implementation for ONE requirement:\n\n"
        "  1. `cd /workspace`\n"
        "  2. `git add -A`\n"
        "  3. `git diff --cached > requirement_patches/<slug>.diff`\n"
        "  4. `git commit -m \"impl: <slug>\"`\n\n"
        "Where `<slug>` is the requirement filename without the `.yaml` extension.\n"
        "The patch file MUST be non-empty for the requirement to count as complete.\n"
        "Never write or `touch` an empty `.diff` file.\n\n"
        "You may run `git log`, `git diff`, `git show` at any time to inspect what\n"
        "prior rounds have done — only your own commits will be there; no reference\n"
        "solution is accessible.\n"
    )

    def _init_workspace_git(self, terminal: Terminal) -> None:
        """Init /workspace as a git repo with a single 'round 0' commit.

        Tasks ship without a ``.git/`` in ``base/``, so this is the only history
        the agent can ever see — no path to gold-patch leakage. The regression
        harness uses a *separate* hidden ``GIT_DIR=/var/lib/lhb/git`` and is not
        affected by this repo.
        """
        container = terminal._container
        if container is None:
            return
        cmd = (
            "cd /workspace && "
            "rm -rf .git && "
            "git init -q && "
            'git config user.email "agent@lhb.local" && '
            'git config user.name "lhb-agent" && '
            "git config commit.gpgsign false && "
            "git add -A && "
            'git -c core.autocrlf=false commit -q --allow-empty '
            '-m "round 0: initial workspace state"'
        )
        try:
            exit_code, output = container.exec_run(["bash", "-lc", cmd])
        except Exception as exc:  # pragma: no cover - defensive
            self._logger.warning(f"workspace git init raised: {exc}")
            return
        if exit_code != 0:
            tail = output.decode("utf-8", errors="replace")[-400:] if isinstance(output, (bytes, bytearray)) else str(output)[-400:]
            self._logger.warning(f"workspace git init failed (rc={exit_code}): {tail}")

    def _persist_workspace_git(self, terminal: Terminal, handler: TrialHandler) -> None:
        """Write git_log.txt + workspace_git.bundle into agent-logs/."""
        container = terminal._container
        if container is None:
            return
        out_dir = handler.trial_paths.agent_logging_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            rc, out = container.exec_run(
                ["bash", "-lc", "cd /workspace && git log --all --stat --date=iso 2>&1"]
            )
            text = out.decode("utf-8", errors="replace") if isinstance(out, (bytes, bytearray)) else str(out or "")
            if rc != 0:
                text = f"<git log failed rc={rc}>\n{text}"
            (out_dir / "git_log.txt").write_text(text)
        except Exception as exc:  # pragma: no cover
            self._logger.warning(f"persist git_log.txt failed: {exc}")

        try:
            in_container = "/tmp/lhb_workspace.bundle"
            rc, out = container.exec_run(
                ["bash", "-lc", f"cd /workspace && git bundle create {in_container} --all 2>&1"]
            )
            if rc != 0:
                tail = out.decode("utf-8", errors="replace")[-400:] if isinstance(out, (bytes, bytearray)) else str(out)[-400:]
                self._logger.warning(f"git bundle failed: {tail}")
                return
            bits, _ = container.get_archive(in_container)
            buf = io.BytesIO(b"".join(bits))
            buf.seek(0)
            with tarfile.open(fileobj=buf, mode="r|") as tf:
                for member in tf:
                    if not member.isfile():
                        continue
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        continue
                    (out_dir / "workspace_git.bundle").write_bytes(extracted.read())
                    break
        except Exception as exc:  # pragma: no cover
            self._logger.warning(f"persist workspace_git.bundle failed: {exc}")

    def _maybe_start_regression_harness(
        self, handler: TrialHandler, terminal: Terminal
    ) -> tuple[threading.Thread | None, RegressionHarness | None]:
        """Kick off DAG-triggered regression testing in a daemon thread."""
        if self._agent_name == AgentName.ORACLE:
            return None, None
        if terminal.tester_container is None:
            return None, None

        tests_dir = handler.task_paths.test_dir
        run_tests_sh = handler.task_paths.run_tests_path
        if not tests_dir.exists() or not run_tests_sh.exists():
            return None, None

        try:
            cfg = RegressionHarnessConfig(
                task_dir=str(handler.task_dir),
                agent_container=handler.client_container_name,
                tester_container=handler.tester_container_name,
                poll_interval_sec=REGRESSION_POLL_INTERVAL_SEC,
                max_wall_sec=handler.task.max_agent_timeout_sec,
                test_timeout_sec=int(handler.task.max_test_timeout_sec),
                output_dir=str(handler.trial_paths.task_output_path / "regression"),
            )
            harness = RegressionHarness(cfg)
        except Exception as exc:
            self._logger.warning(
                f"{handler.task_id}: failed to build RegressionHarness: {exc}"
            )
            return None, None

        def _loop() -> None:
            try:
                harness.loop()
            except Exception as exc:
                self._logger.warning(
                    f"{handler.task_id}: regression harness loop crashed: {exc}"
                )

        t = threading.Thread(target=_loop, name=f"reg-{handler.task_id}", daemon=True)
        t.start()
        return t, harness

    def _read_regression_summary(
        self, regression_dir: Path, handler: TrialHandler
    ) -> RegressionResults | None:
        iterations_path = regression_dir / "observation_history.jsonl"
        if not iterations_path.exists():
            return None

        mode = "unknown"
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        passed_units: set[str] = set()
        last_iteration_ts: str | None = None
        snapshot_count = 0

        try:
            for raw_line in iterations_path.read_text().splitlines():
                if not raw_line.strip():
                    continue
                iteration = json.loads(raw_line)
                mode = iteration.get("mode") or mode
                last_iteration_ts = iteration.get("ts") or last_iteration_ts
                if not iteration.get("triggered"):
                    continue
                snapshot_count += 1
                passed_tests.update(iteration.get("passed_tests", []))
                failed_tests.update(iteration.get("failed_tests", []))
                passed_units.update(iteration.get("passed_units", []))
        except Exception as exc:
            self._logger.debug(
                f"reg iterations read failed for {handler.task_id}: {exc}"
            )
            return None

        return RegressionResults(
            mode=mode,
            passed_tests=sorted(passed_tests),
            failed_tests=sorted(failed_tests),
            passed_units=sorted(passed_units),
            last_iteration_ts=last_iteration_ts,
            snapshot_count=snapshot_count,
        )

    def _stop_regression_harness(
        self,
        thread: threading.Thread | None,
        harness: RegressionHarness | None,
        handler: TrialHandler,
    ) -> RegressionResults | None:
        if thread is None or harness is None:
            return None
        harness.stop()
        thread.join(timeout=30.0)
        return self._read_regression_summary(Path(harness.out_dir), handler)

    def _collect_container_dir(
        self,
        handler: TrialHandler,
        src: str,
        dest: Path,
        label: str,
    ) -> None:
        import subprocess

        check = subprocess.run(
            [
                *docker_command(["exec", handler.client_container_name, "sh", "-c"]),
                f"test -d {src} && echo present",
            ],
            capture_output=True,
            text=True,
            env=docker_env(),
        )
        if "present" not in check.stdout:
            return
        dest.mkdir(parents=True, exist_ok=True)
        cp = subprocess.run(
            [*docker_command(["cp", f"{handler.client_container_name}:{src}/.", str(dest)])],
            capture_output=True,
            text=True,
            env=docker_env(),
        )
        if cp.returncode != 0:
            self._logger.warning(
                f"{handler.task_id}: failed to collect {label}: {cp.stderr[:200]}"
            )
            return
        try:
            count = sum(1 for _ in dest.rglob("*") if _.is_file())
            self._logger.info(
                f"{handler.task_id}: collected {count} {label} file(s)"
            )
        except Exception:
            pass

    def _collect_agent_tests(
        self, handler: TrialHandler
    ) -> None:
        """Copy /workspace/agent_tests/ from the client container to trial logs.

        This preserves any tests the agent chose to write (per the prompt
        convention) for later analysis. Silently no-op if the directory is
        absent or the copy fails — the agent is not required to write tests.
        """
        self._collect_container_dir(
            handler=handler,
            src="/workspace/agent_tests",
            dest=handler.trial_paths.task_output_path / "agent_tests",
            label="agent_tests",
        )

    def _collect_agent_artifacts(
        self, handler: TrialHandler
    ) -> None:
        self._collect_container_dir(
            handler=handler,
            src="/workspace/agent_plans",
            dest=handler.trial_paths.agent_plans_path,
            label="agent_plans",
        )
        self._collect_container_dir(
            handler=handler,
            src="/workspace/requirement_patches",
            dest=handler.trial_paths.requirement_patches_path,
            label="requirement_patches",
        )

    def _copy_container_file(
        self,
        handler: TrialHandler,
        src: str,
        dest: Path,
        label: str,
        container_name: str | None = None,
    ) -> bool:
        container_name = container_name or handler.client_container_name
        check = subprocess.run(
            [
                *docker_command(["exec", container_name, "sh", "-c"]),
                f"test -f {src} && echo present",
            ],
            capture_output=True,
            text=True,
            env=docker_env(),
        )
        if "present" not in check.stdout:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        cp = subprocess.run(
            [*docker_command(["cp", f"{container_name}:{src}", str(dest)])],
            capture_output=True,
            text=True,
            env=docker_env(),
        )
        if cp.returncode != 0:
            self._logger.warning(
                f"{handler.task_id}: failed to collect {label}: {cp.stderr[:200]}"
            )
            return False
        return True

    def _resolve_result_source_path(self, source_path: str) -> str:
        path = Path(source_path)
        if path.is_absolute():
            return str(path)
        return str(Path("/workspace") / path)

    def _collect_result_artifacts(
        self,
        handler: TrialHandler,
        results: TrialResults,
        post_test_pane: str,
    ) -> list[ResultArtifact]:
        artifacts: list[ResultArtifact] = []
        results.raw_output_paths = {
            "pre_agent": str(handler.trial_paths.pre_agent_pane_path),
            "post_agent": str(handler.trial_paths.post_agent_pane_path),
            "post_test": str(handler.trial_paths.post_test_pane_path),
        }
        artifacts.append(
            ResultArtifact(
                name="post-test",
                kind="text",
                format=ResultFormat.TERMINAL_TEXT.value,
                path=str(handler.trial_paths.post_test_pane_path),
                found=True,
            )
        )

        for source in handler.task.get_result_sources():
            if source.format == ResultFormat.TERMINAL_TEXT.value:
                continue
            if not source.path:
                continue

            source_container_path = self._resolve_result_source_path(source.path)
            artifact_name = Path(source.path).name
            artifact_dest = handler.trial_paths.task_output_path / artifact_name
            collected = False
            if handler.tester_container_name is not None:
                collected = self._copy_container_file(
                    handler,
                    source_container_path,
                    artifact_dest,
                    artifact_name,
                    container_name=handler.tester_container_name,
                )
            if not collected:
                collected = self._copy_container_file(
                    handler,
                    source_container_path,
                    artifact_dest,
                    artifact_name,
                )
            artifacts.append(
                ResultArtifact(
                    name=artifact_name,
                    kind=artifact_dest.suffix.lstrip(".") or "file",
                    format=source.format,
                    path=str(artifact_dest),
                    found=collected,
                    required=source.required,
                )
            )

        results.result_artifacts = artifacts
        return artifacts

    def _parse_result_artifacts(
        self,
        handler: TrialHandler,
        artifacts: list[ResultArtifact],
        post_test_pane: str,
    ) -> tuple[list[ParsedResultSet], FailureMode]:
        artifact_by_format = {artifact.format: artifact for artifact in artifacts}
        parsed_sets: list[ParsedResultSet] = []
        for source in handler.task.get_result_sources():
            artifact = artifact_by_format.get(source.format)
            if artifact is None:
                if source.required:
                    self._logger.error(
                        f"Missing required result source {source.format} for {handler.task_id}"
                    )
                    return parsed_sets, FailureMode.PARSE_ERROR
                continue
            if not artifact.found:
                if source.required:
                    self._logger.error(
                        f"Required result artifact not found for {handler.task_id}: {source.path or source.format}"
                    )
                    return parsed_sets, FailureMode.PARSE_ERROR
                continue
            try:
                parser = ParserFactory.get_parser(source.format)
                context = ParseContext(
                    source_name=artifact.name,
                    format=artifact.format,
                    artifact_path=artifact.path,
                    raw_text=(post_test_pane if artifact.format == ResultFormat.TERMINAL_TEXT.value else None),
                    task_id=handler.task_id,
                    is_fallback=source.fallback,
                )
                parsed_sets.append(parser.parse(context))
            except Exception as exc:
                artifact.parse_error = str(exc)
                if source.required:
                    self._logger.error(
                        f"Error parsing required results for {handler.task_id}: {exc}"
                    )
                    return parsed_sets, FailureMode.PARSE_ERROR
        return parsed_sets, FailureMode.NONE

    def _merge_result_sets(
        self,
        parsed_sets: list[ParsedResultSet],
    ) -> dict[str, UnitTestStatus] | None:
        final_results: dict[str, UnitTestStatus] = {}
        case_level_sets = [parsed_set for parsed_set in parsed_sets if not parsed_set.is_summary_only]
        summary_only_sets = [parsed_set for parsed_set in parsed_sets if parsed_set.is_summary_only]

        for parsed_set in [*case_level_sets, *summary_only_sets]:
            for case in parsed_set.cases:
                if case.id == "__summary__" and case_level_sets:
                    continue
                final_results.setdefault(case.id, case.status)
        return final_results or None

    def _sync_workspace_to_tester(
        self, terminal: Terminal, handler: TrialHandler
    ) -> None:
        tester = terminal.tester_container
        if tester is None:
            return

        try:
            tester.exec_run(
                [
                    "sh",
                    "-lc",
                    "find /workspace -mindepth 1 -maxdepth 1 -exec rm -rf {} +",
                ]
            )
            bits, _ = terminal._container.get_archive("/workspace")
            archive = b"".join(bits)
            tester.put_archive("/", archive)
        except Exception as exc:
            self._logger.warning(
                f"{handler.task_id}: failed to sync workspace to tester: {exc}"
            )

    def _run_tests(
        self,
        terminal: Terminal,
        session,
        handler: TrialHandler,
    ) -> FailureMode:
        timeout = (
            self._global_test_timeout_sec
            or handler.task.max_test_timeout_sec * self._global_timeout_multiplier
        )

        if terminal.tester_container is None:
            self._logger.warning(
                f"{handler.task_id}: tester container is required for direct test execution"
            )
            return FailureMode.UNKNOWN_AGENT_ERROR

        self._sync_workspace_to_tester(terminal, handler)

        from long_horizon_bench.harness.test_runner import TestRunner

        runner = TestRunner(
            tester_container=handler.tester_container_name,
            workspace_mount="/workspace",
            tests_dir=str(DockerComposeManager.CONTAINER_TEST_DIR),
        )
        result = runner.run(
            snapshot_path="/workspace",
            timeout_sec=int(timeout),
        )

        post_test_text = ""
        if result.raw_stderr:
            post_test_text += f"# --- stderr ---\n{result.raw_stderr}\n"
        post_test_text += result.raw_stdout
        if post_test_text and not post_test_text.endswith("\n"):
            post_test_text += "\n"
        handler.trial_paths.post_test_pane_path.write_text(post_test_text)

        if result.exit_code == 124:
            self._logger.warning(
                f"Tests timed out after {timeout}s for {handler.task_id}"
            )
            return FailureMode.TEST_TIMEOUT
        if result.exit_code != 0:
            self._logger.info(
                f"{handler.task_id}: direct run-tests.sh exited with {result.exit_code}"
            )
        return FailureMode.NONE

    def _is_resolved(
        self, parser_results: dict[str, UnitTestStatus] | None
    ) -> bool:
        if not parser_results:
            return False
        tail_id = tail_case_id()
        if tail_id in parser_results:
            return parser_results[tail_id] == UnitTestStatus.PASSED
        if set(parser_results) == {"__summary__"}:
            return False
        statuses = list(parser_results.values())
        if not any(s == UnitTestStatus.PASSED for s in statuses):
            return False
        return all(
            r in (UnitTestStatus.PASSED, UnitTestStatus.SKIPPED)
            for r in statuses
        )

    def _log_failed_tests(
        self,
        handler: TrialHandler,
        parser_results: dict[str, UnitTestStatus] | None,
    ) -> None:
        if not parser_results:
            return
        failed_tests = [
            test_name
            for test_name, status in parser_results.items()
            if status in (UnitTestStatus.FAILED, UnitTestStatus.ERROR)
        ]
        if failed_tests:
            self._logger.info(
                f"{handler.task_id}: failed tests: {', '.join(sorted(failed_tests))}"
            )

    def _extract_trajectories(
        self,
        terminal: Terminal,
        agent: BaseAgent,
        logging_dir: Path,
    ) -> dict[str, object]:
        """Copy trajectory files from the agent (client) container to host.

        Always uses the client container regardless of which session is active,
        because traj files are written by the agent inside the client container.
        """
        traj_paths = agent.get_trajectory_paths()
        if not traj_paths:
            return {
                "expected": False,
                "requested": [],
                "extracted": [],
                "missing": [],
                "errors": [],
            }
        container = terminal._container  # client container
        traj_dir = logging_dir / "trajectories"
        traj_dir.mkdir(parents=True, exist_ok=True)
        extracted: list[str] = []
        missing: list[str] = []
        errors: list[str] = []
        for cpath in traj_paths:
            try:
                bits, _ = container.get_archive(cpath)
                data = b"".join(bits)
                with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                    tar.extractall(traj_dir)
                extracted.append(cpath)
                self._logger.info(
                    f"Extracted trajectory: {cpath} -> {traj_dir}"
                )
            except Exception as exc:
                message = str(exc)
                if "Not Found" in message:
                    missing.append(cpath)
                    self._logger.warning(
                        f"Missing trajectory for agent output: {cpath}"
                    )
                else:
                    errors.append(f"{cpath}: {message}")
                    self._logger.warning(
                        f"Could not extract trajectory {cpath}: {exc}"
                    )
        return {
            "expected": True,
            "requested": traj_paths,
            "extracted": extracted,
            "missing": missing,
            "errors": errors,
        }

    def _classify_missing_trajectory(
        self,
        agent_result: AgentResult | None,
        extraction: dict[str, object],
        current_failure_mode: FailureMode,
    ) -> FailureMode:
        if not extraction.get("expected"):
            return current_failure_mode
        if extraction.get("extracted"):
            return current_failure_mode
        if current_failure_mode == FailureMode.AGENT_TIMEOUT:
            return current_failure_mode
        if agent_result is None:
            return FailureMode.AGENT_STARTUP_ERROR
        if current_failure_mode in (FailureMode.UNSET, FailureMode.NONE, FailureMode.UNKNOWN_AGENT_ERROR):
            return FailureMode.AGENT_STARTUP_ERROR
        return current_failure_mode

    # -- agent execution --

    async def _run_agent_with_timeout(
        self,
        session,
        logging_dir: Path,
        timeout_sec: float,
        agent: BaseAgent,
        instruction: str,
    ) -> AgentResult | None:
        return agent.perform_task(
            instruction=instruction,
            session=session,
            logging_dir=logging_dir,
            timeout_sec=timeout_sec,
        )

    def _run_agent(
        self,
        session,
        handler: TrialHandler,
        agent: BaseAgent,
        instruction: str,
        *,
        logging_dir: Path | None = None,
        timeout_sec: float | None = None,
    ) -> tuple[AgentResult | None, FailureMode]:
        timeout = timeout_sec or (
            self._global_agent_timeout_sec
            or handler.task.max_agent_timeout_sec * self._global_timeout_multiplier
        )
        logging_dir = logging_dir or handler.trial_paths.agent_logging_dir

        try:
            result = agent.perform_task(
                instruction=instruction,
                session=session,
                logging_dir=logging_dir,
                timeout_sec=timeout,
            )
            if result is None:
                return None, FailureMode.UNKNOWN_AGENT_ERROR
            return result, FailureMode(result.failure_mode)
        except Exception as exc:
            self._logger.error(
                f"Error running agent for {handler.task_id}: {exc}"
            )
            return None, FailureMode.UNKNOWN_AGENT_ERROR

    def _run_agent_with_rate_limit_retries(
        self,
        session,
        handler: TrialHandler,
        agent: BaseAgent,
        instruction: str,
        *,
        logging_dir: Path,
        timeout_sec: float | None,
    ) -> tuple[AgentResult | None, FailureMode]:
        """Run the agent; on ``agent_rate_limited``, backoff and retry within this outer-loop round.

        After ``rate_limit_retries_per_round`` extra attempts (see env / ``-k``), return the last
        outcome. ``AGENT_RATE_LIMITED`` remains retryable at the outer-loop level so the harness
        can start the next round (new shell session) instead of ending the trial immediately.
        """
        max_attempts = 1 + self._rate_limit_retries_per_round
        agent_result: AgentResult | None = None
        agent_fm = FailureMode.NONE
        for attempt in range(max_attempts):
            attempt_dir = (
                logging_dir if attempt == 0 else logging_dir / f"rate-limit-retry-{attempt}"
            )
            attempt_dir.mkdir(parents=True, exist_ok=True)
            agent_result, agent_fm = self._run_agent(
                session,
                handler,
                agent,
                instruction,
                logging_dir=attempt_dir,
                timeout_sec=timeout_sec,
            )
            if agent_fm != FailureMode.AGENT_RATE_LIMITED:
                return agent_result, agent_fm
            if attempt >= max_attempts - 1:
                break
            delay = self._rate_limit_backoff_sec * (attempt + 1)
            self._logger.warning(
                "%s: agent hit API rate limit (attempt %s/%s); sleeping %.1fs before retry",
                handler.task_id,
                attempt + 1,
                max_attempts,
                delay,
            )
            if delay > 0:
                time.sleep(delay)
        return agent_result, agent_fm

    def _requirement_slugs(self, handler: TrialHandler) -> list[str]:
        requirements_dir = handler.task_dir / "requirements"
        if not requirements_dir.is_dir():
            return []
        return sorted(path.stem for path in requirements_dir.glob("*.yaml"))

    def _completed_requirement_slugs(self, handler: TrialHandler) -> list[str]:
        patches_dir = handler.trial_paths.requirement_patches_path
        if not patches_dir.is_dir():
            return []
        return sorted(
            path.stem
            for path in patches_dir.glob("*.diff")
            if path.is_file() and path.stat().st_size > 0
        )

    def _outer_loop_unbounded(self) -> bool:
        """True when ``outer_loop_count`` should not cap harness rounds.

        Use ``-k outer_loop_count=0``, ``-1``, or ``-k outer_loop_count=unlimited``
        (case-insensitive). Rounds still stop on full requirement coverage, a
        non-retryable agent failure, or the trial agent time budget.
        """
        raw = self._agent_kwargs.get("outer_loop_count", 3)
        if isinstance(raw, str):
            s = raw.strip().lower()
            if s in {"unlimited", "unbounded", "inf", "none", "no-limit"}:
                return True
        try:
            return int(raw) <= 0  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False

    def _outer_loop_count(self) -> int:
        """Maximum outer-loop index when bounded (default 3).

        When :meth:`_outer_loop_unbounded` is true, the harness loop does not use
        this as a cap; see ``_run_trial`` ``while`` condition.
        """
        raw_value = self._agent_kwargs.get("outer_loop_count", 3)
        try:
            return max(1, int(raw_value))
        except (TypeError, ValueError):
            return 3

    def _round_logging_dir(
        self,
        handler: TrialHandler,
        cycle_index: int,
        outer_loop_count: int,
        *,
        unbounded_outer_loops: bool = False,
    ) -> Path:
        base_dir = handler.trial_paths.agent_logging_dir
        if not unbounded_outer_loops and outer_loop_count <= 1:
            return base_dir
        fmt = f"{cycle_index:04d}" if unbounded_outer_loops else f"{cycle_index:02d}"
        return base_dir / "outer-loop" / f"round-{fmt}"

    def _outer_loop_history_path(self, handler: TrialHandler) -> Path:
        return handler.trial_paths.agent_logging_dir / "outer_loop_history.jsonl"

    def _record_outer_loop_round(
        self,
        handler: TrialHandler,
        *,
        cycle_index: int,
        logging_dir: Path,
        timeout_sec: float,
        failure_mode: FailureMode,
        progress_before: dict[str, Any],
        progress_after: dict[str, Any],
    ) -> None:
        history_path = self._outer_loop_history_path(handler)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            relative_logging_dir = logging_dir.relative_to(handler.trial_paths.task_output_path)
        except ValueError:
            relative_logging_dir = logging_dir
        payload = {
            "cycle_index": cycle_index,
            "logging_dir": str(relative_logging_dir),
            "timeout_sec": timeout_sec,
            "failure_mode": failure_mode.value,
            "progress_before": progress_before,
            "progress_after": progress_after,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        with history_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")

    def _promote_round_artifacts(self, round_logging_dir: Path, base_logging_dir: Path) -> None:
        if round_logging_dir == base_logging_dir or not round_logging_dir.exists():
            return
        for relative_path in (
            Path("claude_sdk_result.json"),
            Path("trajectories") / "claude_sdk_trajectory.jsonl",
            Path("trajectories") / "qwen_code_stream.jsonl",
        ):
            src = round_logging_dir / relative_path
            if not src.is_file():
                continue
            dest = base_logging_dir / relative_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)

    def _round_timeout_sec(
        self,
        remaining_agent_budget: float,
        cycle_index: int,
        outer_loop_count: int,
        *,
        unbounded_outer_loops: bool = False,
    ) -> float:
        if unbounded_outer_loops:
            return max(0.0, remaining_agent_budget)
        remaining_cycles = max(outer_loop_count - cycle_index + 1, 1)
        return remaining_agent_budget / remaining_cycles

    def _is_retryable_agent_failure(self, failure_mode: FailureMode) -> bool:
        """Failures that allow another outer-loop iteration (new agent session).

        Includes ``AGENT_RATE_LIMITED``: after in-round retries are exhausted, start the next
        outer-loop round instead of ending the trial immediately.
        """
        return failure_mode in (
            FailureMode.NONE,
            FailureMode.AGENT_TIMEOUT,
            FailureMode.AGENT_RATE_LIMITED,
        )

    def _progress_state(self, handler: TrialHandler) -> dict[str, Any]:
        total = self._requirement_slugs(handler)
        completed = sorted(
            set(self._completed_requirement_slugs(handler)).intersection(total)
        )
        blocked = sorted(set(total) - set(completed))
        percent = 100.0 if not total else (len(completed) / len(total)) * 100.0
        return {
            "total_requirements": total,
            "completed_requirements": completed,
            "remaining_requirements": blocked,
            "progress_percent": percent,
        }

    def _build_progress_instruction(
        self,
        base_instruction: str,
        progress: dict[str, Any],
    ) -> str:
        total = progress["total_requirements"]
        completed = progress["completed_requirements"]
        remaining = progress["remaining_requirements"]
        if not total:
            return base_instruction

        lines = [
            base_instruction,
            "",
            "Harness progress update:",
            f"- Requirement coverage: {len(completed)}/{len(total)} ({progress['progress_percent']:.1f}%)",
            f"- Completed requirement slugs: {', '.join(completed) if completed else 'none'}",
            f"- Remaining requirement slugs: {', '.join(remaining) if remaining else 'none'}",
            "- Your previous run ended naturally. Continue from the current workspace state.",
            "- You may estimate progress yourself by comparing `/workspace/requirement_patches/*.diff` against `/workspace/requirements/*.yaml`.",
            "- You are only allowed to end when the whole task is complete, not when one part is complete.",
            "- The full task prompt is repeated above; keep working until every remaining requirement is implemented or you are truly blocked.",
        ]
        return "\n".join(lines) + self.GIT_WORKFLOW_PROMPT

    # -- single trial --

    def _run_trial(self, handler: TrialHandler) -> TrialResults:
        self._logger.debug(f"Running task: {handler.task_id}")

        base_instruction = compose_instruction(handler.instruction, self._agent_name)
        total_agent_budget = (
            self._global_agent_timeout_sec
            or handler.task.max_agent_timeout_sec * self._global_timeout_multiplier
        )

        results = TrialResults(
            trial_name=handler.trial_name,
            task_id=handler.task_id,
            instruction=base_instruction,
        )
        results.trial_started_at = datetime.now(timezone.utc).isoformat()

        parser_results: dict[str, UnitTestStatus] | None = None
        total_input_tokens = 0
        total_output_tokens = 0
        last_agent = None
        last_session = None
        executed_cycles = 0

        compose_files: list[Path] = [handler.docker_compose_path]
        if _REGRESSION_COMPOSE_PATH.is_file():
            compose_files.append(_REGRESSION_COMPOSE_PATH)
        resolved_image = resolve_task_docker_image(
            task_id=handler.task_id,
            strategy=self._docker_image_strategy,
            docker_image_namespace=self._docker_image_namespace,
            docker_image_tag=self._docker_image_tag,
        )
        write_task_image_resolution(
            handler.trial_paths.task_image_resolution_path,
            task_id=handler.task_id,
            client_image_ref=resolved_image.client_image_ref,
            image_source=resolved_image.image_source,
            image_pull_performed=resolved_image.pull_required,
            image_build_performed=resolved_image.build_required,
        )

        with spin_up_terminal(
            client_container_name=handler.client_container_name,
            client_image_name=resolved_image.client_image_ref,
            tester_container_name=handler.tester_container_name,
            tester_image_name=resolved_image.client_image_ref,
            docker_image_name_prefix=handler.docker_image_name_prefix,
            docker_compose_path=compose_files,
            logs_path=handler.trial_paths.logs_path,
            agent_logs_path=handler.trial_paths.agent_logging_dir,
            commands_path=handler.trial_paths.commands_path,
            docker_image_strategy=resolved_image.strategy,
            no_rebuild=self._no_rebuild,
            cleanup=self._cleanup,
            task_dir=handler.task_dir.resolve(),
        ) as terminal:
            first_session = terminal.create_session("agent-1")
            self._init_workspace_git(terminal)
            pre_pane = first_session.capture_pane(capture_entire=True)
            handler.trial_paths.pre_agent_pane_path.write_text(pre_pane)

            patch_thread, patch_stop_event = self._start_patch_timeline_recorder(
                handler, terminal
            )
            reg_thread, reg_harness = self._maybe_start_regression_harness(
                handler, terminal
            )

            try:
                deadline = time.monotonic() + total_agent_budget

                if self._agent_name == AgentName.ORACLE:
                    # Single-shot reference solution: no outer loop, no progress wrapping,
                    # no outer_loop_history.jsonl rounds.
                    session = first_session
                    last_session = session
                    last_agent = self._create_agent_for_task(handler.task_id)
                    remaining_agent_budget = deadline - time.monotonic()
                    if remaining_agent_budget <= 0:
                        results.failure_mode = FailureMode.AGENT_TIMEOUT
                    else:
                        round_timeout_sec = self._round_timeout_sec(
                            remaining_agent_budget=remaining_agent_budget,
                            cycle_index=1,
                            outer_loop_count=1,
                            unbounded_outer_loops=False,
                        )
                        round_logging_dir = self._round_logging_dir(
                            handler,
                            cycle_index=1,
                            outer_loop_count=1,
                            unbounded_outer_loops=False,
                        )
                        if results.agent_started_at is None:
                            results.agent_started_at = datetime.now(
                                timezone.utc
                            ).isoformat()
                        agent_result, agent_fm = (
                            self._run_agent_with_rate_limit_retries(
                                session,
                                handler,
                                last_agent,
                                base_instruction,
                                logging_dir=round_logging_dir,
                                timeout_sec=round_timeout_sec,
                            )
                        )
                        results.agent_ended_at = datetime.now(timezone.utc).isoformat()

                        post_agent = session.capture_pane(capture_entire=True)
                        handler.trial_paths.post_agent_pane_path.write_text(post_agent)

                        executed_cycles += 1
                        if agent_result is not None:
                            total_input_tokens += agent_result.total_input_tokens
                            total_output_tokens += agent_result.total_output_tokens
                        results.total_input_tokens = total_input_tokens
                        results.total_output_tokens = total_output_tokens
                        results.failure_mode = (
                            agent_fm if agent_fm != FailureMode.NONE else FailureMode.NONE
                        )

                        extraction = self._extract_trajectories(
                            terminal, last_agent, round_logging_dir
                        )
                        self._promote_round_artifacts(
                            round_logging_dir, handler.trial_paths.agent_logging_dir
                        )
                        results.failure_mode = self._classify_missing_trajectory(
                            agent_result,
                            extraction,
                            results.failure_mode,
                        )

                        self._collect_agent_artifacts(handler)
                else:
                    cycle_index = 1
                    unbounded_outer = self._outer_loop_unbounded()
                    bounded_rounds = self._outer_loop_count()
                    progress = self._progress_state(handler)
                    while unbounded_outer or cycle_index <= bounded_rounds:
                        progress_before = progress
                        session = (
                            first_session
                            if cycle_index == 1
                            else terminal.create_session(f"agent-{cycle_index}")
                        )
                        last_session = session
                        last_agent = self._create_agent_for_task(handler.task_id)
                        remaining_agent_budget = deadline - time.monotonic()
                        instruction = self._build_progress_instruction(
                            base_instruction, progress_before
                        )
                        if remaining_agent_budget <= 0:
                            results.failure_mode = FailureMode.AGENT_TIMEOUT
                            break

                        round_timeout_sec = self._round_timeout_sec(
                            remaining_agent_budget=remaining_agent_budget,
                            cycle_index=cycle_index,
                            outer_loop_count=bounded_rounds,
                            unbounded_outer_loops=unbounded_outer,
                        )
                        round_logging_dir = self._round_logging_dir(
                            handler,
                            cycle_index=cycle_index,
                            outer_loop_count=bounded_rounds,
                            unbounded_outer_loops=unbounded_outer,
                        )

                        if results.agent_started_at is None:
                            results.agent_started_at = datetime.now(
                                timezone.utc
                            ).isoformat()
                        agent_result, agent_fm = (
                            self._run_agent_with_rate_limit_retries(
                                session,
                                handler,
                                last_agent,
                                instruction,
                                logging_dir=round_logging_dir,
                                timeout_sec=round_timeout_sec,
                            )
                        )
                        results.agent_ended_at = datetime.now(timezone.utc).isoformat()

                        post_agent = session.capture_pane(capture_entire=True)
                        handler.trial_paths.post_agent_pane_path.write_text(post_agent)

                        executed_cycles += 1
                        if agent_result is not None:
                            total_input_tokens += agent_result.total_input_tokens
                            total_output_tokens += agent_result.total_output_tokens
                        results.total_input_tokens = total_input_tokens
                        results.total_output_tokens = total_output_tokens
                        results.failure_mode = (
                            agent_fm if agent_fm != FailureMode.NONE else FailureMode.NONE
                        )

                        extraction = self._extract_trajectories(
                            terminal, last_agent, round_logging_dir
                        )
                        self._promote_round_artifacts(
                            round_logging_dir, handler.trial_paths.agent_logging_dir
                        )
                        results.failure_mode = self._classify_missing_trajectory(
                            agent_result,
                            extraction,
                            results.failure_mode,
                        )

                        self._collect_agent_artifacts(handler)
                        progress = self._progress_state(handler)
                        self._record_outer_loop_round(
                            handler,
                            cycle_index=cycle_index,
                            logging_dir=round_logging_dir,
                            timeout_sec=round_timeout_sec,
                            failure_mode=results.failure_mode,
                            progress_before=progress_before,
                            progress_after=progress,
                        )
                        if progress["progress_percent"] >= 100.0:
                            break
                        if not self._is_retryable_agent_failure(results.failure_mode):
                            break
                        cycle_index += 1

                if last_session is None:
                    last_session = first_session
                if last_agent is None:
                    last_agent = self._create_agent_for_task(handler.task_id)

                extraction = self._extract_trajectories(
                    terminal, last_agent, handler.trial_paths.agent_logging_dir
                )
                if results.failure_mode in (FailureMode.UNSET, FailureMode.NONE):
                    results.failure_mode = self._classify_missing_trajectory(
                        None,
                        extraction,
                        results.failure_mode,
                    )

                test_session = last_session
                if not handler.task.run_tests_in_same_shell:
                    test_session = terminal.create_session(
                        f"tests-{max(executed_cycles, 1)}",
                        use_tester=terminal.tester_container is not None,
                    )

                results.test_started_at = datetime.now(timezone.utc).isoformat()
                test_fm = self._run_tests(terminal, test_session, handler)
                results.test_ended_at = datetime.now(timezone.utc).isoformat()
                post_test = handler.trial_paths.post_test_pane_path.read_text()

                if test_fm != FailureMode.NONE:
                    if results.failure_mode == FailureMode.UNSET:
                        results.failure_mode = test_fm
                else:
                    artifacts = self._collect_result_artifacts(handler, results, post_test)
                    parsed_result_sets, parse_fm = self._parse_result_artifacts(
                        handler, artifacts, post_test
                    )
                    results.parsed_result_sets = parsed_result_sets
                    if parse_fm != FailureMode.NONE:
                        results.failure_mode = parse_fm
                    else:
                        parser_results = self._merge_result_sets(parsed_result_sets)
                        results.final_test_results = (
                            {k: v.value for k, v in parser_results.items()}
                            if parser_results
                            else None
                        )
                        results.parser_results = results.final_test_results
                        results.is_resolved = self._is_resolved(parser_results)
                        if results.is_resolved:
                            results.failure_mode = FailureMode.NONE
            finally:
                reg_results = self._stop_regression_harness(
                    reg_thread, reg_harness, handler
                )
                self._stop_patch_timeline_recorder(
                    patch_thread, patch_stop_event, handler, terminal
                )
                results.regression_results = reg_results
                self._collect_agent_tests(handler)
                self._collect_agent_artifacts(handler)
                self._persist_workspace_git(terminal, handler)

        if results.is_resolved:
            self._logger.debug(f"Resolved task {handler.task_id}")
        else:
            self._logger.debug(f"Unresolved task {handler.task_id}")
            self._log_failed_tests(handler, parser_results)

        results.trial_ended_at = datetime.now(timezone.utc).isoformat()
        return results

    # -- orchestration --

    def _execute_single_trial(
        self, trial_name: str, task_path: Path
    ) -> TrialResults:
        try:
            handler = TrialHandler(
                trial_name=trial_name,
                input_path=task_path,
                output_path=self._run_path,
            )
        except Exception as exc:
            self._logger.error(
                f"Failed to construct TrialHandler for {task_path}: {exc}",
                exc_info=True,
            )
            return TrialResults(
                trial_name=trial_name,
                task_id=task_path.name,
                instruction="",
                failure_mode=FailureMode.UNKNOWN_AGENT_ERROR,
            )
        try:
            result = self._run_trial(handler)
            return result
        except Exception as exc:
            failure_mode = (
                FailureMode.AGENT_STARTUP_ERROR
                if isinstance(exc, TerminalStartupError)
                else FailureMode.UNKNOWN_AGENT_ERROR
            )
            self._logger.error(
                f"Harness execution failed for {task_path}: {exc}",
                exc_info=True,
            )
            return TrialResults(
                trial_name=trial_name,
                task_id=handler.task_id,
                instruction=handler.instruction,
                failure_mode=failure_mode,
            )

    def _get_trial_name(self, task_path: Path, attempt: int) -> str:
        return f"{task_path.name}.{attempt}-of-{self._n_attempts}.{self._run_id}"

    def _load_previous_results_list(self) -> list[TrialResults]:
        if not self._is_resuming or not self._results_output_path.exists():
            return []
        try:
            combined = BenchmarkResults.model_validate_json(
                self._results_output_path.read_text()
            )
            return combined.results
        except Exception as exc:
            self._logger.warning(
                f"Failed to load {self._results_output_path}: {exc}"
            )
            return []

    def _load_previous_results(self) -> BenchmarkResults | None:
        all_results = self._load_previous_results_list()
        if not all_results:
            return None
        combined = BenchmarkResults(results=all_results)
        self._logger.info(
            f"Loaded {len(all_results)} previous results "
            f"(accuracy: {combined.accuracy:.2%})"
        )
        return combined

    def _execute_tasks(self) -> BenchmarkResults:
        results = BenchmarkResults()

        if self._is_resuming:
            prev = self._load_previous_results()
            if prev:
                results.results = prev.results

        if len(self._dataset) == 0:
            self._logger.info("No tasks remaining to run.")
            self._write_results(results)
            return results

        max_workers = min(len(self._dataset), self._n_concurrent_trials)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for task_path in self._dataset:
                for attempt in range(1, self._n_attempts + 1):
                    trial_name = self._get_trial_name(task_path, attempt)
                    fut = executor.submit(
                        self._execute_single_trial,
                        trial_name=trial_name,
                        task_path=task_path,
                    )
                    futures[fut] = trial_name

            total = len(self._dataset) * self._n_attempts

            if self._livestream:
                for fut in as_completed(futures):
                    try:
                        results.results.append(fut.result())
                    except Exception as exc:
                        trial_name = futures.get(fut, "unknown")
                        self._logger.error(
                            f"Unhandled trial future error for {trial_name}: {exc}",
                            exc_info=True,
                        )
                        results.results.append(
                            TrialResults(
                                trial_name=trial_name,
                                task_id=trial_name.split(".")[0],
                                instruction="",
                                failure_mode=FailureMode.UNKNOWN_AGENT_ERROR,
                            )
                        )
                    self._write_results(results)
            else:
                with Progress(
                    SpinnerColumn(),
                    TextColumn("{task.description}"),
                    BarColumn(),
                    TaskProgressColumn(),
                    TimeElapsedColumn(),
                ) as progress:
                    task = progress.add_task(
                        f"Running tasks (0/{total}, "
                        f"Accuracy: {results.accuracy:.2%})",
                        total=total,
                    )
                    for fut in as_completed(futures):
                        try:
                            trial_result = fut.result()
                        except Exception as exc:
                            trial_name = futures.get(fut, "unknown")
                            self._logger.error(
                                f"Unhandled trial future error for {trial_name}: {exc}",
                                exc_info=True,
                            )
                            trial_result = TrialResults(
                                trial_name=trial_name,
                                task_id=trial_name.split(".")[0],
                                instruction="",
                                failure_mode=FailureMode.UNKNOWN_AGENT_ERROR,
                            )
                        # Deduplicate
                        key = (trial_result.task_id, trial_result.trial_name)
                        existing_keys = {
                            (r.task_id, r.trial_name) for r in results.results
                        }
                        if key not in existing_keys:
                            results.results.append(trial_result)
                        else:
                            for i, er in enumerate(results.results):
                                if (er.task_id, er.trial_name) == key:
                                    results.results[i] = trial_result
                                    break

                        self._write_results(results)

                        done = len(results.results)
                        status = (
                            "✓" if trial_result.is_resolved else "✗"
                        )
                        progress.update(
                            task,
                            advance=1,
                            description=(
                                f"Running tasks ({done}/{total}, "
                                f"Accuracy: {results.accuracy:.2%}) - "
                                f"Last: {trial_result.task_id} {status}"
                            ),
                        )

        return results

    # -- write helpers --

    def _write_results(self, results: BenchmarkResults) -> None:
        self._results_output_path.write_text(
            results.model_dump_json(indent=4)
        )

    def _get_git_commit_hash(self) -> str:
        try:
            return (
                subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
                )
                .decode()
                .strip()
            )
        except Exception:
            return "unknown"

    def _get_user(self) -> str:
        try:
            user = (
                subprocess.check_output(
                    ["git", "config", "user.name"], stderr=subprocess.DEVNULL
                )
                .decode()
                .strip()
            )
            if user:
                return user
        except Exception:
            pass
        try:
            return (
                subprocess.check_output(["whoami"], stderr=subprocess.DEVNULL)
                .decode()
                .strip()
            )
        except Exception:
            return "unknown"

    def _write_run_metadata(self) -> None:
        metadata = build_run_metadata(
            run_id=self._run_id,
            dataset_path=(
                self._dataset._path.absolute()
                if hasattr(self._dataset, "_path") and self._dataset._path
                else None
            ),
            dataset_name=self._dataset_name,
            dataset_version=self._dataset_version,
            output_path=self._output_path,
            agent_name=self._agent_class.name(),
            no_rebuild=self._no_rebuild,
            docker_image_strategy=self._docker_image_strategy,
            docker_image_namespace=self._docker_image_namespace,
            docker_image_tag=self._docker_image_tag,
            cleanup=self._cleanup,
            log_level=self._log_level,
            task_ids=self._dataset.task_ids,
            exclude_task_ids=self._exclude_task_ids,
            n_tasks=self._n_tasks,
            n_concurrent_trials=self._n_concurrent_trials,
            n_attempts=self._n_attempts,
            dataset_size=len(self._dataset),
            model_name=self._model_name,
            commit_hash=self._get_git_commit_hash(),
            username=self._get_user(),
            start_time=self._start_time,
            s3_bucket=lhb_config.s3_bucket_name,
            agent_kwargs=self._agent_kwargs,
        )
        self._run_metadata_output_path.write_text(
            metadata.model_dump_json(indent=4)
        )

    def _update_metadata_on_end(self, results: BenchmarkResults) -> None:
        if self._run_metadata_output_path.exists():
            try:
                metadata = RunMetadata.model_validate_json(
                    self._run_metadata_output_path.read_text()
                )
                metadata.end_time = datetime.now(timezone.utc).isoformat()
                metadata.accuracy = results.accuracy
                metadata.pass_at_k = results.pass_at_k
                self._run_metadata_output_path.write_text(
                    metadata.model_dump_json(indent=4)
                )
            except Exception as exc:
                self._logger.warning(f"Failed to update metadata: {exc}")

    # -- entry point --

    def run(self) -> BenchmarkResults:
        """Run the full harness pipeline.

        Returns:
            Aggregated ``BenchmarkResults``.
        """
        self._logger.info("Starting Long-Horizon-Bench harness run")
        self._logger.info(f"Run ID: {self._run_id}")

        if not self._is_resuming:
            self._write_run_metadata()

        results = self._execute_tasks()
        self._update_metadata_on_end(results)

        return results
