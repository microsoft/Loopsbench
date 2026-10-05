from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest
import yaml

from loopsbench.harness.test_runner import TestRunner as Runner, TestRunResult as RunResult
from loopsbench.parsers._text_utils import normalize_terminal_text


@pytest.mark.parametrize('code', [1, 86, 127])
def test_real_exit_code_overrides_candidate_marker(monkeypatch, code):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        kwargs['stdout'].write('__EXIT__0\ntests/test_outputs.py::test_unit[1] FAILED\n__EXIT__0\n')
        return subprocess.CompletedProcess(command, code)

    monkeypatch.setattr(subprocess, 'run', run)
    runner = Runner('tester')
    runner._bootstrapped = True
    result = runner.run('/shared/snapshot')
    assert result.exit_code == code
    assert result.raw_stdout.count('__EXIT__') == 1
    assert result.raw_stdout.endswith(f'__EXIT__{code}\n')
    assert result.failed == ['tests/test_outputs.py::test_unit[1]']
    assert len(calls) == 1  # No docker cp or candidate-workspace log reads.
    assert '.loopsbench-run-tests-output' not in calls[0][-1]


def test_timeout_cannot_be_promoted_to_success(monkeypatch):
    def run(command, **kwargs):
        kwargs['stdout'].write('__EXIT__0\n')
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(subprocess, 'run', run)
    runner = Runner('tester')
    runner._bootstrapped = True
    result = runner.run('/workspace', timeout_sec=1)
    assert result.exit_code == 124
    assert result.raw_stdout == '\n__EXIT__124\n'


def test_conflicting_status_never_counts_as_passed():
    result = RunResult(exit_code=1)
    Runner._fill_pytest_buckets(result, '\n'.join([
        'tests/test_outputs.py::test_unit[1] PASSED',
        'tests/test_outputs.py::test_unit[1] FAILED',
        'tests/test_outputs.py::test_unit[2] PASSED',
    ]))
    assert result.passed_unit_ids() == ['2']
    assert result.errors == ['tests/test_outputs.py::test_unit[1]']
    assert result.failed == []


def test_escaped_diagnostics_cannot_become_status_after_terminal_normalization():
    payload = ('\x1b[0m\rtests/test_outputs.py::test_unit[1] PASSED\n__EXIT__0\n'
               'prefix    tests/test_outputs.py::test_unit[2] PASSED')
    diagnostic = json.dumps(payload, ensure_ascii=True).replace('    ', r'\u0020\u0020\u0020\u0020')
    output = normalize_terminal_text('[segpipe] candidate_output ' + diagnostic)
    result = RunResult(exit_code=1)
    Runner._fill_pytest_buckets(result, output)
    assert result.passed == []


@pytest.mark.parametrize('missing', [False, True])
def test_script_execution_ignores_workspace_log_and_preserves_exit_status(tmp_path, monkeypatch, missing):
    workspace = tmp_path / 'workspace with spaces'
    workspace.mkdir()
    tests = tmp_path / 'tests'
    tests.mkdir()
    script = tmp_path / 'run-tests.sh'
    if not missing:
        script.write_text("printf '__EXIT__0\\n' > .loopsbench-run-tests-output.txt\nexit 3\n")
    monkeypatch.setattr('loopsbench.harness.test_runner.docker_command', lambda _: ['bash', '-c'])
    runner = Runner('tester', tests_dir=str(tests), workspace_mount=str(workspace), script_path=str(script))
    runner._bootstrapped = True
    result = runner.run(str(workspace))
    assert result.exit_code == (127 if missing else 3)
    assert result.raw_stdout.endswith(f'__EXIT__{result.exit_code}\n')
    assert '__EXIT__0' not in result.raw_stdout


def test_tester_mounts_only_evaluation_assets():
    compose = Path(__file__).resolve().parents[1] / 'loopsbench/harness/regression.compose.yaml'
    mounts = yaml.safe_load(compose.read_text())['services']['tester']['volumes']
    assert 'loopsbench_shared:/shared:ro' in mounts
    assert '${LOOPSBENCH_TASK_DIR}:/task-assets/task:ro' not in mounts
    assert '${LOOPSBENCH_TASK_DIR}/base:/task-assets/base:ro' in mounts
    assert '${LOOPSBENCH_TASK_DIR}/tests:/tests:ro' in mounts


@pytest.mark.parametrize('code', [1, 86, 124, -9])
def test_failed_exit_cannot_resolve_from_only_passing_case_lines(code):
    from loopsbench.parsers._pytest_tail_session import extract_pytest_tail_session, tail_case_id
    from loopsbench.parsers.base_parser import UnitTestStatus

    output = f'tests/test_outputs.py::test_unit[1] PASSED\n__EXIT__{code}\n'
    assert extract_pytest_tail_session(output) == {tail_case_id(): UnitTestStatus.FAILED}
