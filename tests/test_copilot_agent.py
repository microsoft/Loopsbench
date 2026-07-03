from long_horizon_bench.agents.agent_factory import AgentFactory
from long_horizon_bench.agents.agent_name import AgentName
from long_horizon_bench.agents.base_agent import AgentResult
from long_horizon_bench.agents.copilot.copilot_agent import CopilotAgent


def test_copilot_agent_is_registered_builtin() -> None:
    assert AgentName.COPILOT.value == "copilot"

    agent_cls = AgentFactory.get_agent_class(AgentName.COPILOT)

    assert agent_cls.name() == "copilot"


def test_copilot_agent_builds_cli_command_for_ssh_responses_provider() -> None:
    agent = CopilotAgent(model_name="mai-code-1-flash-internal")

    command = agent._build_copilot_command("Fix the task")

    assert "COPILOT_PROVIDER_BASE_URL=http://10.100.0.1:4146/v1" in command
    assert "COPILOT_PROVIDER_TYPE=openai" in command
    assert "COPILOT_PROVIDER_WIRE_API=responses" in command
    assert "COPILOT_MODEL=mai-code-1-flash-internal" in command
    assert "COPILOT_PROVIDER_WIRE_MODEL=mai-code-1-flash-internal" in command
    assert "copilot" in command
    assert "--model mai-code-1-flash-internal" in command
    assert "--allow-all" in command
    assert "--no-remote" in command



def test_copilot_agent_perform_task_returns_agent_result(monkeypatch) -> None:
    agent = CopilotAgent(model_name="mai-code-1-flash-internal")
    sent_commands = []

    class FakeSession:
        def send_keys(self, command, block=False, max_timeout_sec=None):
            sent_commands.append((command, block, max_timeout_sec))

    monkeypatch.setattr(agent, "_ensure_copilot_cli", lambda session: None)

    result = agent.perform_task("Fix the task", FakeSession(), timeout_sec=1.0)

    assert isinstance(result, AgentResult)
    assert sent_commands
    assert sent_commands[0][1] is True
    assert sent_commands[0][2] == 1.0
