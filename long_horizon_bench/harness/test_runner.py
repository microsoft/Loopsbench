"""Run task-provided test scripts inside the tester container against a snapshot."""
from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from long_horizon_bench.parsers._pytest_nested_filter import should_skip_nested_pytest_nodeid
from long_horizon_bench.parsers._text_utils import normalize_terminal_text
from long_horizon_bench.terminal.docker_compose_manager import docker_command, docker_env

# Written inside the tester workspace; collected via ``docker cp`` so logs are not
# truncated by host-side ``subprocess`` capture limits.
TEST_OUTPUT_LOG_NAME = ".lhb-run-tests-output.txt"


@dataclass
class TestRunResult:
    exit_code: int
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    raw_stdout: str = ""
    raw_stderr: str = ""

    def passed_unit_ids(self) -> list[str]:
        out = []
        for name in self.passed:
            m = re.search(r"test_unit\[([^\]]+)\]", name)
            if m:
                out.append(m.group(1))
        return out


class TestRunner:
    """Runs a task's ``run-tests.sh`` inside the tester container."""

    def __init__(
        self,
        tester_container: str,
        tests_dir: str = "/tests",
        workspace_mount: str = "/workspace",
        script_path: str = "/task-assets/run-tests.sh",
    ) -> None:
        self.tester_container = tester_container
        self.tests_dir = tests_dir
        self.workspace_mount = workspace_mount
        self.script_path = script_path
        self._bootstrapped = False

    def _container_test_log_path(self) -> str:
        return (Path(self.workspace_mount) / TEST_OUTPUT_LOG_NAME).as_posix()

    def _docker_cp_from_container(self, container_path: str, host_path: Path) -> bool:
        cp = subprocess.run(
            [
                *docker_command(
                    ["cp", f"{self.tester_container}:{container_path}", str(host_path)]
                )
            ],
            capture_output=True,
            text=True,
            env=docker_env(),
        )
        return cp.returncode == 0 and host_path.is_file()

    def _read_test_log_from_container(self, exec_stdout: str, exec_stderr: str) -> tuple[str, str]:
        """Return (combined_log_text, docker_exec_stderr) from container log file.

        Falls back to *exec_stdout* when ``docker cp`` fails (e.g. container died).
        """
        log_path = self._container_test_log_path()
        tmp = tempfile.NamedTemporaryFile(
            prefix="lhb-test-log-", suffix=".txt", delete=False
        )
        tmp_path = Path(tmp.name)
        tmp.close()
        try:
            if self._docker_cp_from_container(log_path, tmp_path):
                text = tmp_path.read_text(encoding="utf-8", errors="replace")
                return text, exec_stderr
        finally:
            tmp_path.unlink(missing_ok=True)
        # Preserve any docker-exec stdout (wrapper messages) ahead of captured stdout.
        merged = exec_stdout or ""
        if exec_stderr:
            merged = f"# --- docker exec stderr ---\n{exec_stderr}\n" + merged
        return merged, exec_stderr

    def bootstrap(self) -> None:
        if self._bootstrapped:
            return
        script = (
            "set -e; "
            "command -v rsync >/dev/null || "
            "((apt-get update -qq && apt-get install -y -qq rsync >/dev/null) || true); "
            "test -x /bin/bash || test -x /usr/bin/bash"
        )
        subprocess.run(
            [*docker_command(["exec", self.tester_container, "bash", "-c"]), script],
            capture_output=True,
            text=True,
            timeout=300,
            env=docker_env(),
        )
        self._bootstrapped = True

    def run(
        self,
        snapshot_path: str,
        timeout_sec: int = 600,
        mode: str = "run_tests_sh",
    ) -> TestRunResult:
        self.bootstrap()
        prepare_workspace = ""
        if snapshot_path != self.workspace_mount:
            prepare_workspace = (
                f"find {self.workspace_mount} -mindepth 1 -maxdepth 1 -exec rm -rf {{}} + 2>/dev/null || true; "
                f"rsync -a {snapshot_path}/ {self.workspace_mount}/ 2>/dev/null || "
                f"cp -a {snapshot_path}/. {self.workspace_mount}/; "
            )
        script = (
            f"{prepare_workspace}"
            f"cd {self.workspace_mount} && (git init -q 2>/dev/null; git add -A 2>/dev/null; "
            f"git -c user.email=b@b -c user.name=b commit -q -m snap --allow-empty 2>/dev/null) || true; "
            f"RUN_TESTS_SCRIPT={self.script_path}; "
            f"test -f \"$RUN_TESTS_SCRIPT\" || RUN_TESTS_SCRIPT={self.tests_dir}/run-tests.sh; "
            f"export REPO_DIR={self.workspace_mount}; "
            f"export TEST_DIR={self.tests_dir}; "
            f"export UNIT_TEST_DIR={self.tests_dir}; "
            f"export REPO_DIR_OVERRIDE={self.workspace_mount}; "
            f"LHB_TEST_LOG={self.workspace_mount}/{TEST_OUTPUT_LOG_NAME}; "
            f"rm -f \"$LHB_TEST_LOG\"; "
            f"if ! test -f \"$RUN_TESTS_SCRIPT\"; then "
            f"echo 'lhb: missing run-tests (expected {self.script_path} or {self.tests_dir}/run-tests.sh)' "
            f">\"$LHB_TEST_LOG\"; echo __EXIT__127 >>\"$LHB_TEST_LOG\"; exit 0; fi; "
            f"chmod +x \"$RUN_TESTS_SCRIPT\" 2>/dev/null || true; "
            f"set +e; bash \"$RUN_TESTS_SCRIPT\" >\"$LHB_TEST_LOG\" 2>&1; ec=$?; "
            f"echo \"__EXIT__${{ec}}\" >>\"$LHB_TEST_LOG\"; exit \"$ec\""
        )
        cmd = [*docker_command(["exec", self.tester_container, "bash", "-c"]), script]
        r: subprocess.CompletedProcess[str] | None = None
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                env=docker_env(),
            )
        except subprocess.TimeoutExpired as e:
            stderr_parts = [str(e.stderr or "").rstrip(), "lhb: subprocess timeout"]
            stderr = "\n".join(p for p in stderr_parts if p)
            raw_out, stderr = self._read_test_log_from_container(
                str(e.stdout or ""),
                stderr,
            )
            out = normalize_terminal_text(raw_out)
            result = TestRunResult(
                exit_code=124,
                raw_stdout=out,
                raw_stderr=stderr or "timeout",
            )
            self._fill_pytest_buckets(result, out)
            return result

        assert r is not None
        raw_out, stderr = self._read_test_log_from_container(r.stdout or "", r.stderr or "")
        exit_code = r.returncode
        m = re.search(r"__EXIT__(\d+)", raw_out)
        if m:
            exit_code = int(m.group(1))

        out = normalize_terminal_text(raw_out)

        result = TestRunResult(
            exit_code=exit_code,
            raw_stdout=out,
            raw_stderr=stderr,
        )

        self._fill_pytest_buckets(result, out)
        return result

    @staticmethod
    def _fill_pytest_buckets(result: TestRunResult, out: str) -> None:
        buckets = {
            "PASSED": result.passed,
            "FAILED": result.failed,
            "ERROR": result.errors,
            "SKIPPED": result.skipped,
        }
        forward_re = re.compile(r"(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED)")
        reverse_re = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED)\s+(\S+::\S+)")
        pairs: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            m = forward_re.match(line)
            if m:
                nodeid, status = m.group(1), m.group(2)
            else:
                m = reverse_re.match(line)
                if not m:
                    continue
                status, nodeid = m.group(1), m.group(2)
            key = (nodeid, status)
            if key in seen:
                continue
            seen.add(key)
            pairs.append((nodeid, status))

        cohort = frozenset(n for n, _ in pairs)
        for nodeid, status in pairs:
            if should_skip_nested_pytest_nodeid(nodeid, cohort):
                continue
            buckets[status].append(nodeid)

