"""Factory for creating agent instances."""

from __future__ import annotations

import importlib

from loopsbench.agents.agent_name import AgentName
from loopsbench.agents.base_agent import BaseAgent


class AgentFactory:
    """Registry and factory for agent classes."""

    _AGENT_IMPORTS: dict[AgentName, tuple[str, str]] = {
        AgentName.ORACLE: ("loopsbench.agents.oracle_agent", "OracleAgent"),
        AgentName.MINI_SWE_AGENT: (
            "loopsbench.agents.mini_swe_agent.mini_swe_agent",
            "MiniSweAgent",
        ),
        AgentName.SWE_AGENT: ("loopsbench.agents.swe_agent.swe_agent", "SweAgent"),
        AgentName.OPENHANDS: (
            "loopsbench.agents.openhands.openhands_agent",
            "OpenHandsAgent",
        ),
        AgentName.CLAUDE_CODE: (
            "loopsbench.agents.claude_code.claude_code_agent",
            "ClaudeCodeAgent",
        ),
        AgentName.CURSOR: ("loopsbench.agents.cursor.cursor_agent", "CursorAgent"),
        AgentName.CODEX: ("loopsbench.agents.codex.codex_agent", "CodexAgent"),
        AgentName.QWEN_CODE: (
            "loopsbench.agents.qwen_code.qwen_code_agent",
            "QwenCodeAgent",
        ),
        AgentName.COPILOT: (
            "loopsbench.agents.copilot.copilot_agent",
            "CopilotAgent",
        ),
        AgentName.EXTERNAL_COMMAND: (
            "loopsbench.agents.external_command_agent",
            "ExternalCommandAgent",
        ),
    }

    @classmethod
    def _load_built_in_agent_class(
        cls, agent_name: AgentName
    ) -> type[BaseAgent] | None:
        module_spec = cls._AGENT_IMPORTS.get(agent_name)
        if module_spec is None:
            return None

        module_path, class_name = module_spec
        module = importlib.import_module(module_path)
        agent_cls = getattr(module, class_name)
        if not issubclass(agent_cls, BaseAgent):
            raise ValueError(
                f"Built-in agent `{agent_name.value}` resolved to "
                f"`{module_path}:{class_name}`, which is not a BaseAgent subclass."
            )
        return agent_cls

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
            agent_cls = cls._load_built_in_agent_class(agent_name)
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
