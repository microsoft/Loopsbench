"""Enumeration of built-in agent names."""

from enum import Enum


class AgentName(str, Enum):
    """Identifiers for built-in agents."""

    ORACLE = "oracle"
    MINI_SWE_AGENT = "mini-swe-agent"
    SWE_AGENT = "swe-agent"
    OPENHANDS = "openhands"
    CLAUDE_CODE = "claude-code"
    CURSOR = "cursor"
    CODEX = "codex"
    QWEN_CODE = "qwen-code"
    COPILOT = "copilot"
