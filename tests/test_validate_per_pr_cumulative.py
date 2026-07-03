from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_validate_per_pr():
    return _load_module(SCRIPTS_DIR / "validate_per_pr.py", "lhb_validate_per_pr_cumulative")


def test_validate_tested_pr_strict_cumulative_uses_current_prefix_state(monkeypatch, tmp_path):
    validate_per_pr = _load_validate_per_pr()

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "gold_patches").mkdir()
    (task_dir / "gold_patches" / "c.diff").write_text("diff --git a/x b/x\n")

    state = ["a", "b"]
    observed_states: list[tuple[str, ...]] = []

    monkeypatch.setattr(validate_per_pr, "get_pr_test_files", lambda *_args, **_kwargs: ["tests/test_c.py"])
    monkeypatch.setattr(validate_per_pr, "resolve_patch_host_path", lambda *_args, **_kwargs: task_dir / "gold_patches" / "c.diff")
    monkeypatch.setattr(validate_per_pr, "get_patch_touched_files", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(validate_per_pr, "_get_explicit_selected_tests", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(validate_per_pr, "stage_pr_tests", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(validate_per_pr, "restore_pr_tests", lambda *_args, **_kwargs: (0, "", ""))
    monkeypatch.setattr(validate_per_pr, "_get_repo_head_commit", lambda *_args, **_kwargs: "prefix-head")

    def fake_reset_repo_to_commit(*_args, **kwargs):
        assert kwargs["commit"] == "prefix-head"
        state[:] = ["a", "b"]
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(validate_per_pr, "_reset_repo_to_commit", fake_reset_repo_to_commit)

    def fake_run_pr_tests(_container, _lang, _repo_id, _repo_root, _task_dir, _pr_num, _test_files, _timeout, test_labels=None):
        del test_labels
        snapshot = tuple(state)
        observed_states.append(snapshot)
        if snapshot == ("a", "b"):
            state.append("before-artifact")
        passed = snapshot == ("a", "b", "c")
        return {
            "passed": passed,
            "returncode": 0 if passed else 1,
            "stdout": "",
            "stderr": "",
            "command": ["fake"],
        }

    monkeypatch.setattr(validate_per_pr, "run_pr_tests", fake_run_pr_tests)

    def fake_apply_current_pr_patch(*_args, **kwargs):
        assert kwargs["pr_num"] == "c"
        state.append("c")
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "applied",
            "stderr": "",
            "after_patch_chain": ["c"],
        }

    monkeypatch.setattr(validate_per_pr, "_apply_current_pr_patch", fake_apply_current_pr_patch)

    result = validate_per_pr.validate_tested_pr_strict_cumulative(
        container="container",
        repo_root="/workspace",
        lang="python",
        repo_id="repo",
        task_dir=task_dir,
        pr_num="c",
        test_timeout=300,
    )

    assert observed_states == [("a", "b"), ("a", "b", "c")]
    assert state == ["a", "b", "c"]
    assert result["status"] == "pass"
    assert result["patch_apply"]["after_patch_chain"] == ["c"]


def test_validate_tested_pr_strict_cumulative_runs_after_even_for_p2p(monkeypatch, tmp_path):
    validate_per_pr = _load_validate_per_pr()

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "gold_patches").mkdir()
    (task_dir / "gold_patches" / "c.diff").write_text("diff --git a/x b/x\n")

    state = ["a", "b"]
    observed_states: list[tuple[str, ...]] = []
    apply_calls: list[str] = []

    monkeypatch.setattr(validate_per_pr, "get_pr_test_files", lambda *_args, **_kwargs: ["tests/test_c.py"])
    monkeypatch.setattr(validate_per_pr, "resolve_patch_host_path", lambda *_args, **_kwargs: task_dir / "gold_patches" / "c.diff")
    monkeypatch.setattr(validate_per_pr, "get_patch_touched_files", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(validate_per_pr, "_get_explicit_selected_tests", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(validate_per_pr, "stage_pr_tests", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(validate_per_pr, "restore_pr_tests", lambda *_args, **_kwargs: (0, "", ""))
    monkeypatch.setattr(validate_per_pr, "_get_repo_head_commit", lambda *_args, **_kwargs: "prefix-head")

    def fake_reset_repo_to_commit(*_args, **kwargs):
        assert kwargs["commit"] == "prefix-head"
        state[:] = ["a", "b"]
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(validate_per_pr, "_reset_repo_to_commit", fake_reset_repo_to_commit)

    def fake_run_pr_tests(_container, _lang, _repo_id, _repo_root, _task_dir, _pr_num, _test_files, _timeout, test_labels=None):
        del test_labels
        observed_states.append(tuple(state))
        return {
            "passed": True,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "command": ["fake"],
        }

    monkeypatch.setattr(validate_per_pr, "run_pr_tests", fake_run_pr_tests)

    def fake_apply_current_pr_patch(*_args, **kwargs):
        apply_calls.append(kwargs["pr_num"])
        state.append(kwargs["pr_num"])
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "applied",
            "stderr": "",
            "after_patch_chain": [kwargs["pr_num"]],
        }

    monkeypatch.setattr(validate_per_pr, "_apply_current_pr_patch", fake_apply_current_pr_patch)

    result = validate_per_pr.validate_tested_pr_strict_cumulative(
        container="container",
        repo_root="/workspace",
        lang="python",
        repo_id="repo",
        task_dir=task_dir,
        pr_num="c",
        test_timeout=300,
    )

    assert observed_states == [("a", "b"), ("a", "b", "c")]
    assert apply_calls == ["c"]
    assert result["status"] == "p2p"
    assert result["after"]["passed"] is True


def test_validate_tested_pr_strict_cumulative_uses_strict_after_chain_for_after_state(monkeypatch, tmp_path):
    validate_per_pr = _load_validate_per_pr()

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "gold_patches").mkdir()
    (task_dir / "gold_patches" / "c.diff").write_text("diff --git a/x b/x\n")
    (task_dir / "gold_patches" / "d.diff").write_text("diff --git a/x b/x\n")

    state = ["a", "b"]
    observed_states: list[tuple[str, ...]] = []

    monkeypatch.setattr(validate_per_pr, "get_pr_test_files", lambda *_args, **_kwargs: ["tests/test_c.py"])
    monkeypatch.setattr(
        validate_per_pr,
        "resolve_patch_host_path",
        lambda _task_dir, pr_num: task_dir / "gold_patches" / f"{pr_num}.diff",
    )
    monkeypatch.setattr(validate_per_pr, "get_patch_touched_files", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(validate_per_pr, "_get_explicit_selected_tests", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(validate_per_pr, "stage_pr_tests", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(validate_per_pr, "restore_pr_tests", lambda *_args, **_kwargs: (0, "", ""))
    monkeypatch.setattr(validate_per_pr, "_strict_after_patch_chain", lambda *_args, **_kwargs: ["a", "b", "c", "d"])

    def fake_run_pr_tests(_container, _lang, _repo_id, _repo_root, _task_dir, _pr_num, _test_files, _timeout, test_labels=None):
        del test_labels
        snapshot = tuple(state)
        observed_states.append(snapshot)
        passed = snapshot == ("a", "b", "c", "d")
        return {
            "passed": passed,
            "returncode": 0 if passed else 1,
            "stdout": "",
            "stderr": "",
            "command": ["fake"],
        }

    monkeypatch.setattr(validate_per_pr, "run_pr_tests", fake_run_pr_tests)

    def fake_apply_current_pr_patch(*_args, **kwargs):
        assert kwargs["pr_num"] == "c"
        state.append("c")
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "applied-c",
            "stderr": "",
            "after_patch_chain": ["c"],
        }

    def fake_apply_patch_chain_units(*_args, **kwargs):
        assert kwargs["chain_units"] == ["d"]
        state.append("d")
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "applied-d",
            "stderr": "",
            "after_patch_chain": ["d"],
        }

    commits = iter(["prefix-head", "current-head"])

    def fake_get_repo_head_commit(*_args, **_kwargs):
        return next(commits)

    def fake_reset_repo_to_commit(*_args, **kwargs):
        if kwargs["commit"] == "prefix-head":
            state[:] = ["a", "b"]
        elif kwargs["commit"] == "current-head":
            state[:] = ["a", "b", "c"]
        else:
            raise AssertionError(f"unexpected commit {kwargs['commit']}")
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(validate_per_pr, "_apply_current_pr_patch", fake_apply_current_pr_patch)
    monkeypatch.setattr(validate_per_pr, "_apply_patch_chain_units", fake_apply_patch_chain_units, raising=False)
    monkeypatch.setattr(validate_per_pr, "_get_repo_head_commit", fake_get_repo_head_commit, raising=False)
    monkeypatch.setattr(validate_per_pr, "_reset_repo_to_commit", fake_reset_repo_to_commit, raising=False)

    result = validate_per_pr.validate_tested_pr_strict_cumulative(
        container="container",
        repo_root="/workspace",
        lang="python",
        repo_id="repo",
        task_dir=task_dir,
        pr_num="c",
        test_timeout=300,
    )

    assert observed_states == [("a", "b"), ("a", "b", "c", "d")]
    assert state == ["a", "b", "c"]
    assert result["status"] == "pass"
    assert result["patch_apply"]["after_patch_chain"] == ["a", "b", "c", "d"]


def test_run_strict_pr_sequence_carries_forward_prefix_state(monkeypatch, tmp_path):
    validate_per_pr = _load_validate_per_pr()

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    patch_path = tmp_path / "patch.diff"
    patch_path.write_text("diff --git a/x b/x\n")

    state: list[str] = []
    tested_prefixes: dict[str, tuple[str, ...]] = {}

    monkeypatch.setattr(validate_per_pr, "reset_repo_workspace", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected reset")))
    monkeypatch.setattr(validate_per_pr, "resolve_patch_host_path", lambda *_args, **_kwargs: patch_path)

    def fake_apply_current_pr_patch(*_args, **kwargs):
        pr_num = kwargs["pr_num"]
        state.append(pr_num)
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": "", "after_patch_chain": [pr_num]}

    monkeypatch.setattr(validate_per_pr, "_apply_current_pr_patch", fake_apply_current_pr_patch)

    def fake_validate_tested_pr_strict_cumulative(*_args, **kwargs):
        pr_num = kwargs["pr_num"]
        tested_prefixes[pr_num] = tuple(state)
        state.append(pr_num)
        return {
            "pr": pr_num,
            "tested": True,
            "status": "pass",
            "patch_apply": {"ok": True, "after_patch_chain": [pr_num]},
        }

    monkeypatch.setattr(validate_per_pr, "validate_tested_pr_strict_cumulative", fake_validate_tested_pr_strict_cumulative)

    prs, summary, overall_ok = validate_per_pr._run_strict_pr_sequence_cumulative(
        container_name="container",
        repo_root="/workspace",
        lang="python",
        rid="repo",
        task_dir=task_dir,
        pr_order=["a", "b", "c"],
        tested_prs={"b", "c"},
        test_timeout=300,
        stop_on_fail=False,
        start_pr=None,
    )

    assert tested_prefixes == {"b": ("a",), "c": ("a", "b")}
    assert state == ["a", "b", "c"]
    assert [pr["pr"] for pr in prs] == ["a", "b", "c"]
    assert summary["patch_apply_only"] == 1
    assert summary["strict_pass"] == 2
    assert overall_ok is True


def test_validate_tested_pr_strict_cumulative_stops_if_prefix_restore_fails(monkeypatch, tmp_path):
    validate_per_pr = _load_validate_per_pr()

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "gold_patches").mkdir()
    (task_dir / "gold_patches" / "c.diff").write_text("diff --git a/x b/x\n")

    apply_called = False

    monkeypatch.setattr(validate_per_pr, "get_pr_test_files", lambda *_args, **_kwargs: ["tests/test_c.py"])
    monkeypatch.setattr(validate_per_pr, "resolve_patch_host_path", lambda *_args, **_kwargs: task_dir / "gold_patches" / "c.diff")
    monkeypatch.setattr(validate_per_pr, "get_patch_touched_files", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(validate_per_pr, "_get_explicit_selected_tests", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(validate_per_pr, "stage_pr_tests", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(validate_per_pr, "restore_pr_tests", lambda *_args, **_kwargs: (0, "", ""))
    monkeypatch.setattr(validate_per_pr, "_get_repo_head_commit", lambda *_args, **_kwargs: "prefix-head")
    monkeypatch.setattr(
        validate_per_pr,
        "run_pr_tests",
        lambda *_args, **_kwargs: {"passed": False, "returncode": 1, "stdout": "", "stderr": "", "command": ["fake"]},
    )

    def fake_reset_repo_to_commit(*_args, **kwargs):
        assert kwargs["commit"] == "prefix-head"
        return {"ok": False, "returncode": 1, "stdout": "", "stderr": "reset failed"}

    def fake_apply_current_pr_patch(*_args, **_kwargs):
        nonlocal apply_called
        apply_called = True
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": "", "after_patch_chain": ["c"]}

    monkeypatch.setattr(validate_per_pr, "_reset_repo_to_commit", fake_reset_repo_to_commit)
    monkeypatch.setattr(validate_per_pr, "_apply_current_pr_patch", fake_apply_current_pr_patch)

    result = validate_per_pr.validate_tested_pr_strict_cumulative(
        container="container",
        repo_root="/workspace",
        lang="python",
        repo_id="repo",
        task_dir=task_dir,
        pr_num="c",
        test_timeout=300,
    )

    assert result["status"] == "fail_reset_workspace"
    assert result["failure_stage"] == "reset_workspace"
    assert apply_called is False


def test_validate_tested_pr_strict_cumulative_restores_after_stage_failure(monkeypatch, tmp_path):
    validate_per_pr = _load_validate_per_pr()

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "gold_patches").mkdir()
    (task_dir / "gold_patches" / "c.diff").write_text("diff --git a/x b/x\n")
    (task_dir / "gold_patches" / "d.diff").write_text("diff --git a/x b/x\n")

    stage_calls: list[str] = []
    reset_commits: list[str] = []

    monkeypatch.setattr(validate_per_pr, "get_pr_test_files", lambda *_args, **_kwargs: ["tests/test_c.py"])
    monkeypatch.setattr(
        validate_per_pr,
        "resolve_patch_host_path",
        lambda _task_dir, pr_num: task_dir / "gold_patches" / f"{pr_num}.diff",
    )
    monkeypatch.setattr(validate_per_pr, "get_patch_touched_files", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(validate_per_pr, "_get_explicit_selected_tests", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(validate_per_pr, "restore_pr_tests", lambda *_args, **_kwargs: (0, "", ""))
    monkeypatch.setattr(validate_per_pr, "_strict_after_patch_chain", lambda *_args, **_kwargs: ["a", "b", "c", "d"])

    def fake_stage_pr_tests(_container, _repo_root, _pr_num, _test_files, **_kwargs):
        stage_calls.append("stage")
        if len(stage_calls) == 1:
            return {"ok": True}
        return {"ok": False, "returncode": 1, "stdout": "", "stderr": "after stage failed"}

    monkeypatch.setattr(validate_per_pr, "stage_pr_tests", fake_stage_pr_tests)
    monkeypatch.setattr(
        validate_per_pr,
        "run_pr_tests",
        lambda *_args, **_kwargs: {"passed": False, "returncode": 1, "stdout": "", "stderr": "", "command": ["fake"]},
    )

    commits = iter(["prefix-head", "current-head"])
    monkeypatch.setattr(validate_per_pr, "_get_repo_head_commit", lambda *_args, **_kwargs: next(commits))

    def fake_reset_repo_to_commit(*_args, **kwargs):
        reset_commits.append(kwargs["commit"])
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(validate_per_pr, "_reset_repo_to_commit", fake_reset_repo_to_commit)
    monkeypatch.setattr(
        validate_per_pr,
        "_apply_current_pr_patch",
        lambda *_args, **_kwargs: {"ok": True, "returncode": 0, "stdout": "", "stderr": "", "after_patch_chain": ["c"]},
    )
    monkeypatch.setattr(
        validate_per_pr,
        "_apply_patch_chain_units",
        lambda *_args, **_kwargs: {"ok": True, "returncode": 0, "stdout": "", "stderr": "", "after_patch_chain": ["d"]},
        raising=False,
    )

    result = validate_per_pr.validate_tested_pr_strict_cumulative(
        container="container",
        repo_root="/workspace",
        lang="python",
        repo_id="repo",
        task_dir=task_dir,
        pr_num="c",
        test_timeout=300,
    )

    assert result["status"] == "fail_stage_tests"
    assert result["failure_stage"] == "after_tests"
    assert reset_commits == ["prefix-head", "current-head"]
    assert result["after_restore"]["ok"] is True


def test_run_pr_tests_navidrome_uses_direct_pytest_runner(monkeypatch, tmp_path):
    validate_per_pr = _load_validate_per_pr()

    task_dir = tmp_path / "task_navidrome_segXX"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "tests" / "test_unit_runner.py").write_text("def test_unit():\n    pass\n")

    calls: list[list[str]] = []

    def fake_docker_exec(_container, command, timeout=0):
        calls.append(command)
        del timeout
        return 0, "", ""

    monkeypatch.setattr(validate_per_pr, "docker_exec", fake_docker_exec)
    monkeypatch.setattr(validate_per_pr, "_get_explicit_selected_tests", lambda *_args, **_kwargs: None)

    result = validate_per_pr.run_pr_tests(
        container="container",
        lang="go",
        repo_id="navidrome",
        repo_root="/workspace",
        task_dir=task_dir,
        pr_num="441",
        test_files=["core/archiver_pr441_test.go"],
        timeout=300,
    )

    assert result["passed"] is True
    assert len(calls) == 2
    assert "go mod download all" in calls[0][-1]
    assert "run-tests.sh" not in calls[0][-1]
    assert "-m pytest -v" in calls[1][-1]
    assert "test_unit_runner.py::test_unit[441]" in calls[1][-1]
    assert "export TESTS_DIR=/tests" in calls[1][-1]
    assert "export UNIT_ID=441" in calls[1][-1]
    assert "run-tests.sh" not in calls[1][-1]


def test_apply_patch_chain_units_refreshes_rails_dependencies_for_gemspec_changes(monkeypatch, tmp_path):
    validate_per_pr = _load_validate_per_pr()

    task_dir = tmp_path / "task"
    gold_patches = task_dir / "gold_patches"
    gold_patches.mkdir(parents=True)
    (gold_patches / "56871.diff").write_text("diff --git a/x b/x\n")

    refresh_commands: list[list[str]] = []

    monkeypatch.setattr(
        validate_per_pr,
        "copy_host_path_to_container",
        lambda *_args, **_kwargs: (0, "", ""),
    )
    monkeypatch.setattr(
        validate_per_pr,
        "get_patch_touched_files",
        lambda *_args, **_kwargs: {"actionview/actionview.gemspec"},
    )
    monkeypatch.setattr(
        validate_per_pr,
        "apply_patch_and_commit",
        lambda *_args, **_kwargs: {
            "ok": True,
            "returncode": 0,
            "stdout": "applied",
            "stderr": "",
            "patch_in_container": "/tmp/lhb_patches/current_56871.diff",
            "used_git": True,
        },
    )

    def fake_docker_exec(_container, command, timeout=0):
        del timeout
        refresh_commands.append(command)
        return 0, "refreshed", ""

    monkeypatch.setattr(validate_per_pr, "docker_exec", fake_docker_exec)

    result = validate_per_pr._apply_patch_chain_units(
        container="container",
        repo_root="/workspace",
        repo_id="rails",
        task_dir=task_dir,
        chain_units=["56871"],
    )

    assert result["ok"] is True
    assert len(refresh_commands) == 1
    assert refresh_commands[0][:2] == ["bash", "-c"]
    assert "bundle install --jobs 4 --retry 3" in refresh_commands[0][-1]
