"""Base parser and shared types for test result parsing."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path

from pydantic import BaseModel


def normalize_test_case_id(case_id: str) -> str:
    """Normalize common test identifiers so structured/text sources merge cleanly."""
    case_id = " ".join(case_id.split())
    if "::" not in case_id:
        return case_id

    path_part, remainder = case_id.split("::", 1)
    normalized_path = path_part.replace("\\", "/")
    while normalized_path.startswith("./"):
        normalized_path = normalized_path[2:]
    while normalized_path.startswith("../"):
        normalized_path = normalized_path[3:]
    tests_marker = "/tests/"
    if tests_marker in normalized_path:
        normalized_path = normalized_path.split(tests_marker, 1)[1]
    elif normalized_path.startswith("tests/"):
        normalized_path = normalized_path[len("tests/") :]
    return f"{normalized_path}::{remainder}"


class UnitTestStatus(str, Enum):
    """Status of an individual unit test."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class ParseContext(BaseModel):
    """Context for parsing one result source."""

    source_name: str
    format: str
    artifact_path: str | None = None
    raw_text: str | None = None
    task_id: str | None = None
    is_fallback: bool = False

    def read_text(self) -> str:
        if self.raw_text is not None:
            return self.raw_text
        if self.artifact_path is None:
            return ""
        return Path(self.artifact_path).read_text(encoding="utf-8", errors="replace")


class BaseParser(ABC):
    """Abstract base class for test result parsers."""

    @abstractmethod
    def parse(self, context: ParseContext):
        """Parse one result source into a normalized parsed result set."""
        raise NotImplementedError
