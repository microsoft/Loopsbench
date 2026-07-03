"""Regression harness main loop.

Monitors agent workspace diff size in container A. When accumulated diff meets the
minimum remaining-unit size threshold, triggers snapshot + pytest in container B,
and writes host-side regression observation artifacts.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from long_horizon_bench.harness.unit_tracker import UnitTracker
from long_horizon_bench.harness.snapshot import Snapshotter
from long_horizon_bench.harness.test_runner import TestRunner

log = logging.getLogger("regression_harness")


@dataclass
class HarnessConfig:
    task_dir: str
    agent_container: str
    tester_container: str
    poll_interval_sec: float = 5.0
    min_diff_lines: int = 5
    max_wall_sec: float = 3600.0
    test_timeout_sec: int = 600
    shared_root_agent: str = "/var/lib/lhb/.mirror"
    shared_root_tester: str = "/shared"
    agent_workspace: str = "/workspace"
    tester_workspace: str = "/workspace"
    output_dir: str = "trials"
    trial_name: str | None = None
    # "unit_runner": per-unit pytest via test_unit_runner.py (needs tests/<slug>/).
    # "run_tests_sh": run the task's run-tests.sh against the snapshot.
    test_mode: str = "unit_runner"
    # Only used for run_tests_sh: trigger every N accumulated diff lines.
    standalone_diff_step: int = 80


@dataclass
class IterationRecord:
    ts: str
    diff_lines: int
    threshold: int | None
    snapshot_id: str | None
    triggered: bool
    newly_passed: list[str] = field(default_factory=list)
    newly_failed: list[str] = field(default_factory=list)
    passed_count: int = 0
    failed_count: int = 0
    exit_code: int | None = None
    pytest_log: str | None = None


class RegressionHarness:
    def __init__(self, cfg: HarnessConfig) -> None:
        self.cfg = cfg
        self.tracker = UnitTracker.from_task(cfg.task_dir)
        self.tracker.auto_account_ready_zero_diff_units()
        # If unit_runner mode was requested but no unit has tests, downgrade
        # transparently to run_tests_sh so progressive triggering still works.
        if (
            self.cfg.test_mode == "unit_runner"
            and not self.tracker.tested_units()
            and self.tracker.units
        ):
            log.warning(
                "task %s has no tested units; falling back to run_tests_sh mode",
                Path(cfg.task_dir).name,
            )
            self.cfg.test_mode = "run_tests_sh"
        self.snap = Snapshotter(
            agent_container=cfg.agent_container,
            workspace_dir=cfg.agent_workspace,
            shared_root_agent=cfg.shared_root_agent,
            shared_root_tester=cfg.shared_root_tester,
        )
        self.runner = TestRunner(
            tester_container=cfg.tester_container,
            workspace_mount=cfg.tester_workspace,
        )
        self.history: list[IterationRecord] = []
        self._last_triggered_diff: int = 0
        self._stop_flag: bool = False
        self._prev_passed: set[str] = set()
        self._prev_failed: set[str] = set()
        self._last_passed: set[str] = set()
        self._last_failed: set[str] = set()
        base = Path(cfg.output_dir) / Path(cfg.task_dir).name
        self.out_dir = base / cfg.trial_name if cfg.trial_name else base
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def stop(self) -> None:
        self._stop_flag = True

    def _save_history(self) -> None:
        rows = []
        for it in self.history:
            payload = asdict(it)
            payload["mode"] = self.cfg.test_mode
            rows.append(json.dumps(payload))
        target = self.out_dir / "observation_history.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".observation_history.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(rows) + "\n")
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def run_once(self, force: bool = False) -> IterationRecord:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        diff = self.snap.diff_lines()
        thr = self.tracker.trigger_threshold()
        rec = IterationRecord(ts=ts, diff_lines=diff, threshold=thr, snapshot_id=None, triggered=False)

        dynamic_threshold = max(thr, self.cfg.min_diff_lines) if thr is not None else None
        delta = abs(diff - self._last_triggered_diff)
        if dynamic_threshold is not None:
            trigger = force or (delta >= dynamic_threshold)
        else:
            step = max(self.cfg.standalone_diff_step, self.cfg.min_diff_lines)
            trigger = force or (
                diff >= self.cfg.min_diff_lines and delta >= step
            )
        if not trigger:
            self.history.append(rec)
            return rec
        self._last_triggered_diff = diff

        log.info("triggering snapshot+test (diff=%d, mode=%s, threshold=%s)",
                 diff, self.cfg.test_mode, thr)
        self.snap.ensure_dirs()
        snap = self.snap.take()
        if snap.exit_code != 0:
            task_name = Path(self.cfg.task_dir).name
            log.error(
                "%s: snapshot failed (agent_container=%s): %s",
                task_name,
                self.cfg.agent_container,
                snap.stderr[:500],
            )
            self.history.append(rec)
            return rec
        rec.snapshot_id = snap.snapshot_id
        rec.triggered = True

        result = self.runner.run(
            snap.path,
            timeout_sec=self.cfg.test_timeout_sec,
            mode=self.cfg.test_mode,
        )
        passed_now = set(result.passed_unit_ids())
        failed_now = set(result.failed) | set(result.errors)
        newly_passed_vs_prev = sorted(passed_now - self._prev_passed)
        newly_failed_vs_prev = sorted(self._prev_passed - passed_now)
        self.tracker.mark_passed(passed_now)
        if self.cfg.test_mode != "unit_runner":
            newly_accounted = {
                unit.id
                for unit in self.tracker.ready_units()
                if unit.diff_lines <= diff
            }
            self.tracker.mark_accounted(newly_accounted)
        self.tracker.auto_account_ready_zero_diff_units()
        rec.newly_passed = newly_passed_vs_prev
        rec.newly_failed = newly_failed_vs_prev
        rec.passed_count = len(result.passed)
        rec.failed_count = len(result.failed) + len(result.errors)
        rec.exit_code = result.exit_code
        self._prev_passed = passed_now
        self._prev_failed = failed_now
        self._last_passed = set(result.passed)
        self._last_failed = failed_now

        snap_dir = self.out_dir / "snapshot_runs" / snap.snapshot_id
        snap_dir.mkdir(parents=True, exist_ok=True)
        log_path = snap_dir / "post-test.txt"
        log_path.write_text(
            f"# exit_code={result.exit_code}\n"
            f"# passed={len(result.passed)} failed={len(result.failed)} "
            f"errors={len(result.errors)} skipped={len(result.skipped)}\n"
            f"# --- stderr ---\n{result.raw_stderr}\n"
            f"# --- stdout ---\n{result.raw_stdout}\n"
        )
        rec.pytest_log = str(log_path.relative_to(self.out_dir))

        log.info(
            "test done: passed=%d failed=%d newly_passed=%s newly_failed=%s exit=%s log=%s",
            len(result.passed), len(result.failed) + len(result.errors),
            newly_passed_vs_prev, newly_failed_vs_prev,
            result.exit_code, rec.pytest_log,
        )
        self.history.append(rec)
        self._save_history()
        self.snap.prune(keep=2, keep_ids=[snap.snapshot_id])
        return rec

    def run_final(self) -> dict:
        """Force one final full test run and write regression_summary.json.

        Always triggers regardless of diff threshold so the summary reflects
        the post-agent terminal state. Output schema:
          - regression_tests: tests that passed (acted as regression guard)
          - reproduction_tests: tests that failed (still need reproducing)
        """
        rec = self.run_once(force=True)
        summary = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "passed_count": rec.passed_count,
            "failed_count": rec.failed_count,
            "exit_code": rec.exit_code,
            "snapshot_id": rec.snapshot_id,
            "pytest_log": rec.pytest_log,
            "regression_tests": sorted(self._last_passed),
            "reproduction_tests": sorted(self._last_failed),
            "iterations": len(self.history),
        }
        target = self.out_dir / "regression_summary.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(summary, indent=2) + "\n")
        log.info(
            "final summary: passed=%d failed=%d -> %s",
            rec.passed_count, rec.failed_count, target,
        )
        return summary

    def loop(self) -> None:
        start = time.time()
        self.snap.ensure_dirs()
        while True:
            if self._stop_flag:
                log.info("stop flag set; exiting harness loop")
                break
            if time.time() - start > self.cfg.max_wall_sec:
                log.info("wall clock exceeded; stopping")
                break
            if (
                not self.tracker.remaining_tested_units()
                and self.cfg.test_mode == "unit_runner"
            ):
                log.info("all tested units passed; stopping")
                break
            self.run_once()
            self._save_history()
            # Sleep in small slices so we can react to stop_flag promptly.
            slept = 0.0
            while slept < self.cfg.poll_interval_sec and not self._stop_flag:
                time.sleep(min(1.0, self.cfg.poll_interval_sec - slept))
                slept += 1.0
