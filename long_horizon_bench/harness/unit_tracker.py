"""Track unit diff sizes and remaining (not-yet-passing) units for the regression harness."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


_HUNK_RE = re.compile(r"^[+-](?![+-])")


def count_changed_lines(diff_text: str) -> int:
    n = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if _HUNK_RE.match(line):
            n += 1
    return n


@dataclass
class UnitInfo:
    id: str
    diff_lines: int
    has_tests: bool
    impl_order: int = 0
    predecessors: set[str] = field(default_factory=set)


@dataclass
class UnitTracker:
    task_dir: Path
    units: dict[str, UnitInfo] = field(default_factory=dict)
    passed_units: set[str] = field(default_factory=set)
    accounted_units: set[str] = field(default_factory=set)

    @classmethod
    def from_task(cls, task_dir: str | Path) -> "UnitTracker":
        task_dir = Path(task_dir)
        dag_path = task_dir / "unit_dag.json"
        gold_dir = task_dir / "gold_patches"
        tests_dir = task_dir / "tests"

        has_tests_map: dict[str, bool] = {}
        impl_order_map: dict[str, int] = {}
        predecessors_map: dict[str, set[str]] = {}
        if dag_path.exists():
            dag = json.loads(dag_path.read_text())
            for node in dag.get("nodes", []):
                uid = str(node["id"])
                has_tests_map[uid] = bool(node.get("has_tests", False))
                impl_order_map[uid] = int(node.get("layer", 0))
                predecessors_map.setdefault(uid, set())
            for edge in dag.get("edges", []):
                try:
                    src = str(edge["from"])
                    dst = str(edge["to"])
                except (KeyError, TypeError):
                    continue
                predecessors_map.setdefault(dst, set()).add(src)
                predecessors_map.setdefault(src, set())

        units: dict[str, UnitInfo] = {}
        if gold_dir.is_dir():
            for diff_file in sorted(gold_dir.glob("*.diff")):
                uid = diff_file.stem
                lines = count_changed_lines(diff_file.read_text(errors="replace"))
                test_dir = tests_dir / uid
                has_tests = bool(has_tests_map.get(uid, False))
                if not has_tests and test_dir.is_dir():
                    has_tests = any(test_dir.rglob("*"))
                units[uid] = UnitInfo(
                    id=uid,
                    diff_lines=lines,
                    has_tests=has_tests,
                    impl_order=impl_order_map.get(uid, 0),
                    predecessors=predecessors_map.get(uid, set()),
                )

        # Include DAG-only units that have no diff file (shouldn't happen normally)
        for uid, has_tests in has_tests_map.items():
            if uid not in units:
                units[uid] = UnitInfo(
                    id=uid,
                    diff_lines=0,
                    has_tests=has_tests,
                    impl_order=impl_order_map.get(uid, 0),
                    predecessors=predecessors_map.get(uid, set()),
                )

        return cls(task_dir=task_dir, units=units)

    def tested_units(self) -> list[UnitInfo]:
        return [u for u in self.units.values() if u.has_tests]

    def remaining_tested_units(self) -> list[UnitInfo]:
        return [u for u in self.tested_units() if u.id not in self.passed_units]

    def remaining_accounted_units(self) -> list[UnitInfo]:
        return [u for u in self.units.values() if u.id not in self.accounted_units]

    def mark_passed(self, unit_ids: Iterable[str]) -> None:
        for uid in unit_ids:
            uid = str(uid)
            if uid in self.units:
                self.passed_units.add(uid)
                self.accounted_units.add(uid)

    def mark_accounted(self, unit_ids: Iterable[str]) -> None:
        for uid in unit_ids:
            uid = str(uid)
            if uid in self.units:
                self.accounted_units.add(uid)

    def ready_units(self) -> list[UnitInfo]:
        ready: list[UnitInfo] = []
        for unit in self.remaining_accounted_units():
            if unit.predecessors.issubset(self.accounted_units):
                ready.append(unit)
        return ready

    def auto_account_ready_zero_diff_units(self) -> list[str]:
        accounted: list[str] = []
        while True:
            newly_ready = [
                unit for unit in self.ready_units() if unit.diff_lines <= 0
            ]
            if not newly_ready:
                break
            ids = [unit.id for unit in newly_ready]
            self.mark_accounted(ids)
            accounted.extend(ids)
        return accounted

    def trigger_threshold(self) -> int | None:
        """Minimum positive diff size among remaining ready units. None if none remain."""
        self.auto_account_ready_zero_diff_units()
        rem = self.ready_units()
        positive = [u.diff_lines for u in rem if u.diff_lines > 0]
        if positive:
            return min(positive)
        return None

    def _sorted_ids(self, units: Iterable[UnitInfo]) -> list[str]:
        return [u.id for u in sorted(units, key=lambda u: (u.impl_order, u.id))]

    def summary(self) -> dict:
        return {
            "total_units": len(self.units),
            "tested_units": len(self.tested_units()),
            "passed_units": sorted(self.passed_units),
            "accounted_units": sorted(self.accounted_units),
            "remaining_tested": self._sorted_ids(self.remaining_tested_units()),
            "remaining_accounted": self._sorted_ids(self.remaining_accounted_units()),
            "ready_units": self._sorted_ids(self.ready_units()),
            "trigger_threshold": self.trigger_threshold(),
        }
