"""Factory for creating agent instances."""

from __future__ import annotations

import importlib

from long_horizon_bench.agents.agent_name import AgentName
from long_horizon_bench.agents.base_agent import BaseAgent
from long_horizon_bench.agents.claude_code.claude_code_agent import ClaudeCodeAgent
from long_horizon_bench.agents.codex.codex_agent import CodexAgent
from long_horizon_bench.agents.copilot.copilot_agent import CopilotAgent
from long_horizon_bench.agents.cursor.cursor_agent import CursorAgent
from long_horizon_bench.agents.mini_swe_agent.mini_swe_agent import MiniSweAgent
from long_horizon_bench.agents.openhands.openhands_agent import OpenHandsAgent
from long_horizon_bench.agents.oracle_agent import OracleAgent
from long_horizon_bench.agents.qwen_code.qwen_code_agent import QwenCodeAgent
from long_horizon_bench.agents.swe_agent.swe_agent import SweAgent


class AgentFactory:
    """Registry and factory for agent classes."""

    _AGENTS: dict[AgentName, type[BaseAgent]] = {
        AgentName.ORACLE: OracleAgent,
        AgentName.MINI_SWE_AGENT: MiniSweAgent,
        AgentName.SWE_AGENT: SweAgent,
        AgentName.OPENHANDS: OpenHandsAgent,
        AgentName.CLAUDE_CODE: ClaudeCodeAgent,
        AgentName.CURSOR: CursorAgent,
        AgentName.CODEX: CodexAgent,
        AgentName.QWEN_CODE: QwenCodeAgent,
        AgentName.COPILOT: CopilotAgent,
    }

    @classmethod
    def get_agent_class(
        cls,
        agent_name: AgentName | None = None,
        import_path: str | None = None,
    ) -> type[BaseAgent]:
        """Resolve an agent class from its name or import path.

        Args:
            agent_name: A built-in ``AgentName``.
            import_path: A ``module.path:ClassName`` string for custom agents.

        Returns:
            The agent *class* (not an instance).

        Raises:
            ValueError: If the agent cannot be resolved.
        """
        if import_path is not None:
            module_path, class_name = import_path.rsplit(":", 1)
            module = importlib.import_module(module_path)
            agent_cls = getattr(module, class_name)
            if not issubclass(agent_cls, BaseAgent):
                raise ValueError(
                    f"{import_path} does not point to a BaseAgent subclass"
                )
            return agent_cls

        if agent_name is not None:
            agent_cls = cls._AGENTS.get(agent_name)
            if agent_cls is None:
                raise ValueError(
                    f"Unknown agent: {agent_name}. "
                    f"Available: {', '.join(a.value for a in AgentName)}"
                )
            return agent_cls

        raise ValueError("Either agent_name or import_path must be provided")

    @classmethod
    def get_agent(
        cls,
        agent_name: AgentName | None = None,
        import_path: str | None = None,
        **kwargs,
    ) -> BaseAgent:
        """Create and return an agent instance."""
        agent_cls = cls.get_agent_class(agent_name, import_path)
        return agent_cls(**kwargs)
