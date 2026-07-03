from __future__ import annotations

from long_horizon_bench.harness.instruction_composer import compose_instruction

__all__ = ["Harness", "compose_instruction"]


def __getattr__(name: str):
    if name == "Harness":
        from long_horizon_bench.harness.harness import Harness

        return Harness
    raise AttributeError(name)
