"""Parser for /workspace/results.json artifacts."""

from __future__ import annotations

import json

from long_horizon_bench.harness.models import ParsedResultSet, TestCaseResult
from long_horizon_bench.parsers.base_parser import BaseParser, ParseContext, UnitTestStatus


_PASS_TOKENS = {"pass", "passed", "ok", "success", "succeeded", "true", "通过"}
_FAIL_TOKENS = {"fail", "failed", "failure", "false", "失败"}
_ERROR_TOKENS = {"error", "errored", "exception"}
_SKIP_TOKENS = {"skip", "skipped", "ignored", "pending"}


def _coerce_status(value: object) -> UnitTestStatus:
    """Best-effort interpretation of a results.json value into a UnitTestStatus."""
    if isinstance(value, bool):
        return UnitTestStatus.PASSED if value else UnitTestStatus.FAILED
    if isinstance(value, dict):
        for key in ("status", "result", "outcome", "state"):
            if key in value:
                return _coerce_status(value[key])
        if value.get("passed") is True or value.get("ok") is True:
            return UnitTestStatus.PASSED
        if value.get("failed") is True or value.get("error") is True:
            return UnitTestStatus.FAILED
        return UnitTestStatus.ERROR
    if isinstance(value, (int, float)):
        return UnitTestStatus.PASSED if value else UnitTestStatus.FAILED
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _PASS_TOKENS:
            return UnitTestStatus.PASSED
        if token in _FAIL_TOKENS:
            return UnitTestStatus.FAILED
        if token in _ERROR_TOKENS:
            return UnitTestStatus.ERROR
        if token in _SKIP_TOKENS:
            return UnitTestStatus.SKIPPED
        return UnitTestStatus.ERROR
    return UnitTestStatus.ERROR


class WorkspaceResultsJsonParser(BaseParser):
    """Parse structured workspace results.json into normalized test results."""

    def parse(self, context: ParseContext) -> ParsedResultSet:
        payload = json.loads(context.read_text())
        cases: list[TestCaseResult] = []
        for section_name, section_payload in payload.items():
            if isinstance(section_payload, dict):
                for key, value in section_payload.items():
                    cases.append(
                        TestCaseResult(
                            id=f"{section_name}/{key}",
                            status=_coerce_status(value),
                            source=context.source_name,
                            message=str(value),
                            framework="workspace-results-json",
                        )
                    )
            else:
                cases.append(
                    TestCaseResult(
                        id=str(section_name),
                        status=_coerce_status(section_payload),
                        source=context.source_name,
                        message=str(section_payload),
                        framework="workspace-results-json",
                    )
                )
        summary_counts: dict[str, int] = {}
        for case in cases:
            summary_counts[case.status.value] = summary_counts.get(case.status.value, 0) + 1
        return ParsedResultSet(
            source_name=context.source_name,
            format=context.format,
            cases=cases,
            summary_counts=summary_counts,
            raw_artifact_path=context.artifact_path,
            is_fallback=context.is_fallback,
            is_summary_only=False,
            confidence="case-level",
        )
