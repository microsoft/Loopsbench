"""Terminal-related data models."""

from pydantic import BaseModel


class TerminalCommand(BaseModel):
    """A command to be sent to a tmux session."""

    command: str
    min_timeout_sec: float = 0.0
    max_timeout_sec: float = float("inf")
    block: bool = True
    append_enter: bool = True
