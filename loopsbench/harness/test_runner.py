"""Run task-provided test scripts inside the tester container against a snapshot."""
from __future__ import annotations

import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field

from loopsbench.parsers._pytest_nested_filter import should_skip_nested_pytest_nodeid
from loopsbench.parsers._text_utils import normalize_terminal_text
from loopsbench.terminal.docker_compose_manager import docker_command, docker_env


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
        workspace = shlex.quote(self.workspace_mount)
        snapshot = shlex.quote(snapshot_path)
        prepare_workspace = ""
        if snapshot_path != self.workspace_mount:
            prepare_workspace = (
                f"find {workspace} -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +; "
                f"rsync -a {snapshot}/ {workspace}/ 2>/dev/null || "
                f"cp -a {snapshot}/. {workspace}/; "
            )
        script = (
            f"set -e; {prepare_workspace}"
            f"cd {workspace}; (git init -q 2>/dev/null; git add -A 2>/dev/null; "
            f"git -c core.hooksPath=/dev/null -c user.email=b@b -c user.name=b commit -q -m snap --allow-empty >/dev/null 2>&1) || true; "
            f"RUN_TESTS_SCRIPT={shlex.quote(self.script_path)}; "
            f"test -f \"$RUN_TESTS_SCRIPT\" || RUN_TESTS_SCRIPT={shlex.quote(self.tests_dir + '/run-tests.sh')}; "
            f"export REPO_DIR={workspace}; "
            f"export TEST_DIR={shlex.quote(self.tests_dir)}; "
            f"export UNIT_TEST_DIR={shlex.quote(self.tests_dir)}; "
            f"export REPO_DIR_OVERRIDE={workspace}; "
            "export SEGPIPE_BASE_ROOT=/task-assets/base; "
            f"if ! test -f \"$RUN_TESTS_SCRIPT\"; then "
            f"echo 'loopsbench: missing run-tests (expected {self.script_path} or {self.tests_dir}/run-tests.sh)' "
            f"; exit 127; fi; "
            'exec bash "$RUN_TESTS_SCRIPT"'
        )
        cmd = [*docker_command(["exec", self.tester_container, "bash", "-c"]), script]
        # Stream into a host-owned file. Candidate processes cannot replace a
        # workspace log or redirect it through a symlink before collection.
        stderr = ""
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as log:
            try:
                r = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, text=True,
                                   timeout=timeout_sec, env=docker_env())
                exit_code = r.returncode
            except subprocess.TimeoutExpired:
                exit_code = 124
                stderr = "loopsbench: subprocess timeout"
            log.seek(0)
            out = normalize_terminal_text(log.read())
        # Only the actual docker-exec status may supply the parser's exit marker.
        out = "\n".join(line for line in out.splitlines() if not re.fullmatch(r"__EXIT__-?\d+\s*", line.strip()))
        out = out.rstrip() + f"\n__EXIT__{exit_code}\n"

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

        statuses: dict[str, set[str]] = {}
        for nodeid, status in pairs:
            statuses.setdefault(nodeid, set()).add(status)
        cohort = frozenset(statuses)
        conflicting = {nodeid for nodeid, values in statuses.items() if len(values) > 1}
        for nodeid, status in pairs:
            if should_skip_nested_pytest_nodeid(nodeid, cohort):
                continue
            if nodeid in conflicting:
                if nodeid not in result.errors:
                    result.errors.append(nodeid)
                continue
            buckets[status].append(nodeid)
