from __future__ import annotations

import copy
import json
import logging
import os
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from loopsbench.agents.base_agent import AgentResult, BaseAgent
from loopsbench.file_config import load_run_file
from loopsbench.harness.harness import Harness
from loopsbench.harness.models import FailureMode
from loopsbench.terminal.docker_compose_manager import DockerComposeManager
from loopsbench.terminal.network_policy import (
    MODEL_URL_ENV_KEYS,
    NetworkPlan,
    NetworkPolicy,
    configured_model_hosts,
    controlled_compose,
    model_endpoint_hosts,
)


@pytest.fixture(autouse=True)
def isolate_model_environment(monkeypatch):
    for key in {*MODEL_URL_ENV_KEYS, "LOOPSBENCH_ALLOWED_HOSTS"}:
        monkeypatch.delenv(key, raising=False)


def test_model_urls_automatically_allow_exact_hosts_without_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://user:private-key@Inference.EXAMPLE.org:8443/v1?token=secret")
    monkeypatch.setenv("HTTPS_PROXY", "https://unrelated-proxy.example.org")
    monkeypatch.setenv("REPOSITORY_URL", "https://github.com/source/repo")
    plan = NetworkPlan.from_config(None, agent_kwargs={
        "api_base": "https://inference.example.org/v2",
        "api_key": "must-not-be-recorded",
        "model_kwargs": {"base_url": "http://[2001:db8::5]:8000/v1"},
        "repository_url": "https://github.com/source/repo",
        "mcp_servers": {"search": {"url": "https://search.example.org"}},
    })
    assert plan.agent_policy().allowed_hosts == ["inference.example.org", "2001:db8::5"]
    assert plan.verifier.allowed_hosts == []
    assert plan.environment.allowed_hosts == []
    for secret in ("private-key", "secret", "must-not-be-recorded", "/v1", "8443"):
        assert secret not in plan.model_dump_json()


def test_model_hosts_merge_with_manual_hosts_and_respect_explicit_modes(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://model.example.org/messages")
    monkeypatch.setenv("LOOPSBENCH_ALLOWED_HOSTS", "auth.example.org,model.example.org")
    plan = NetworkPlan.from_config({"agent": {"network_mode": "allowlist", "allowed_hosts": ["extra.example.org"]}})
    assert plan.agent_policy().allowed_hosts == ["extra.example.org", "auth.example.org", "model.example.org"]
    for mode in ("public", "no-network"):
        policy = NetworkPlan.from_config({"agent": {"network_mode": mode}}).agent_policy()
        assert policy.network_mode == mode and policy.allowed_hosts == []


def test_runtime_env_and_custom_model_provider_kwargs_are_recognized(monkeypatch):
    from loopsbench.file_config import apply_model_runtime_config
    from loopsbench.file_config.model_runtime_file import (
        LoopsBenchModelRuntimeFileConfig,
    )

    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example.org")
    apply_model_runtime_config(LoopsBenchModelRuntimeFileConfig(env={"OPENAI_BASE_URL": "https://active.example.org/v1"}))
    hosts = configured_model_hosts({
        "model_providers": {"custom": {"base_url": "https://provider.example.org/v1"}},
        "env": {"LLM_BASE_URL": "https://secondary.example.org", "PIP_INDEX_URL": "https://pypi.example.org"},
    })
    assert hosts == ["active.example.org", "provider.example.org", "secondary.example.org"]


@pytest.mark.parametrize("url", ["file:///private-key", "https://*.example.org/private-key", "https://example.org:bad/private-key", "https://example.org\n/private-key"])
def test_invalid_endpoint_does_not_leak_secrets_in_errors(url):
    with pytest.raises(ValueError) as failure:
        model_endpoint_hosts([url])
    assert "private-key" not in str(failure.value)


def test_policy_validation_and_extra_hosts(monkeypatch):
    monkeypatch.setenv("LOOPSBENCH_ALLOWED_HOSTS", "proxy.local,2001:db8::1")
    plan = NetworkPlan.from_config({"extra_allowed_hosts": ["API.EXAMPLE.COM", "10.2.0.0/16"]})
    assert plan.agent_policy().allowed_hosts == ["api.example.com", "10.2.0.0/16", "proxy.local", "2001:db8::1"]
    assert plan.verifier.network_mode == "no-network"
    for invalid in ("https://api.example.com/v1", "*", "x;touch /tmp/test", "bad\nhost"):
        with pytest.raises(ValueError):
            NetworkPolicy(network_mode="allowlist", allowed_hosts=[invalid])
    with pytest.raises(ValueError):
        NetworkPolicy(network_mode="public", allowed_hosts=["api.example.com"])


def test_compose_preserves_legacy_task_and_moves_only_network_configuration():
    original = {"services": {"client": {"image": "legacy", "command": ["sh", "-c", "echo $VALUE; sleep infinity"],
                            "entrypoint": ["/original-entrypoint"], "network_mode": "none",
                            "volumes": ["/tmp/source:/workspace"], "extra_hosts": ["api.local:host-gateway"]},
                             "tester": {"image": "legacy"}}}
    before = copy.deepcopy(original)
    rendered, controllers = controlled_compose(original, NetworkPlan(), "egress:test")
    assert original == before
    client = rendered["services"]["client"]
    for key in ("command", "entrypoint", "volumes", "image"):
        assert client[key] == original["services"]["client"][key]
    assert client["network_mode"] == "service:loopsbench-egress-client"
    assert controllers["client"] != controllers["tester"]
    assert rendered["services"][controllers["client"]]["extra_hosts"] == ["api.local:host-gateway"]
    assert rendered["services"][controllers["client"]]["networks"]["default"]["aliases"] == ["client"]


def test_run_file_keeps_custom_agent_and_provider_configuration(tmp_path):
    config = tmp_path / "run.yaml"
    config.write_text("""agent_import_path: custom_package.agent:CustomAgent
model: custom_provider/model
agent_kwargs:
  api_base: https://model.example.org/custom/api
network:
  agent:
    network_mode: allowlist
  extra_allowed_hosts: [model.example.org]
""")
    parsed = load_run_file(config)
    assert parsed.agent_import_path == "custom_package.agent:CustomAgent"
    assert parsed.agent_kwargs["api_base"] == "https://model.example.org/custom/api"
    assert parsed.network.agent_policy().allowed_hosts == ["model.example.org"]


@pytest.mark.parametrize("setting", [{"network_mode": "host"}, {"privileged": True}, {"cap_add": ["NET_ADMIN"]}])
def test_incompatible_network_never_silently_runs_unrestricted(setting):
    with pytest.raises(ValueError):
        controlled_compose({"services": {"client": setting}}, NetworkPlan(), "egress:test")


def test_custom_agent_uses_prepare_then_solve_even_on_failure(tmp_path):
    events = []

    class CustomAgent(BaseAgent):
        @staticmethod
        def name():
            return "any-custom-agent"

        def prepare(self, session, logging_dir=None):
            events.append("prepare")

        def perform_task(self, **kwargs):
            events.append("solve")
            raise RuntimeError("expected failure")

    @contextmanager
    def phase(*, model_hosts, uses_host_gateway):
        assert model_hosts == ["custom.example.org"]
        assert uses_host_gateway is False
        events.append("allowlist")
        try:
            yield
        finally:
            events.append("leave")

    harness = Harness.__new__(Harness)
    harness._logger = logging.getLogger(__name__)
    result, failure = harness._run_agent(
        SimpleNamespace(agent_network_phase=phase, prepare_agent=lambda agent, logging_dir: agent.prepare(None, logging_dir)),
        SimpleNamespace(task_id="test"), CustomAgent(base_url="https://custom.example.org/v1"), "fix",
        timeout_sec=10, logging_dir=tmp_path,
    )
    assert events == ["prepare", "allowlist", "solve", "leave"]
    assert result is None and failure == FailureMode.UNKNOWN_AGENT_ERROR

    class LegacyAgent(BaseAgent):
        def __init__(self):
            # Some existing plugins do not call BaseAgent.__init__.
            pass

        @staticmethod
        def name():
            return "legacy"

        def perform_task(self, **kwargs):
            return AgentResult()

    # Old plugins implement only perform_task; no new abstract method is needed.
    assert LegacyAgent().prepare(None) is None
    assert LegacyAgent().model_network_hosts() == []


@pytest.mark.skipif(os.environ.get("LOOPSBENCH_TEST_NETWORK_DOCKER") != "1", reason="opt-in Docker integration")
def test_real_legacy_task_phase_switch_and_separate_verifier(tmp_path):
    image = os.environ.get("LOOPSBENCH_TEST_NETWORK_IMAGE", "python:3.12-slim")
    project = "loopsbench-phase-test-" + tmp_path.name.lower().replace("_", "-")[-24:]
    server = (
        "from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer; "
        "ThreadingHTTPServer(('0.0.0.0',8765),BaseHTTPRequestHandler).serve_forever()"
    )
    data = tmp_path / "legacy-data"
    data.mkdir()
    (data / "sentinel").write_text("legacy-mount")
    service = {"image": image, "entrypoint": ["sh", "-c"],
               "command": ["mkdir -p /workspace; printf '%s' '$$LITERAL' > /tmp/entrypoint-value; exec sleep infinity"],
               "network_mode": "none", "working_dir": "/workspace", "volumes": ["${LOOPSBENCH_TASK_AGENT_LOGS_PATH}:/legacy:ro"]}
    compose = tmp_path / "compose.json"
    compose.write_text(json.dumps({"services": {
        "client": {**service, "container_name": project + "-client"},
        "tester": {**service, "container_name": project + "-tester"},
        "upstream": {"image": image, "command": ["python3", "-c", server],
                     "networks": {"default": {"aliases": ["allowed.test", "blocked.test"]}}},
    }}))
    original = compose.read_bytes()
    manager = DockerComposeManager(
        client_container_name=project + "-client", client_image_name=image,
        tester_container_name=project + "-tester", tester_image_name=image,
        docker_compose_path=compose, no_rebuild=True, logs_path=tmp_path / "logs",
        agent_logs_path=data,
        network=NetworkPlan.from_config(None, agent_kwargs={"base_url": "http://allowed.test:8765/v1", "api_key": "fake-model-key"}),
    )

    def fetch(container, host):
        script = "import socket; s=socket.create_connection((" + repr(host) + ",8765),2); s.settimeout(2); s.sendall(b'GET / HTTP/1.0\\r\\nHost: " + host + "\\r\\n\\r\\n'); assert b'HTTP/' in s.recv(1024)"
        return container.exec_run(["python3", "-c", script])

    live = None
    try:
        client = manager.start()
        assert client.exec_run(["cat", "/tmp/entrypoint-value"]).output == b"$LITERAL"
        assert client.exec_run(["cat", "/legacy/sentinel"]).output == b"legacy-mount"
        assert fetch(client, "blocked.test").exit_code == 0
        assert fetch(manager.tester_container, "allowed.test").exit_code != 0
        # An already-connected socket must not retain preparation-phase access.
        script = "import socket; s=socket.create_connection(('blocked.test',8765),2); s.settimeout(2); print('ready',flush=True); input(); s.sendall(b'GET / HTTP/1.0\\r\\nHost: blocked.test\\r\\n\\r\\n'); print(s.recv(1024),flush=True)"
        live = subprocess.Popen(["docker", "exec", "-i", client.id, "python3", "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert live.stdout.readline().strip() == "ready"
        with manager.agent_network_phase():
            assert fetch(client, "allowed.test").exit_code == 0
            assert fetch(client, "blocked.test").exit_code != 0
            address = manager._service_container("upstream").attrs["NetworkSettings"]["Networks"]
            address = next(iter(address.values()))["IPAddress"]
            assert fetch(client, address).exit_code != 0
            out, _ = live.communicate("go\n", timeout=10)
            assert "HTTP/" not in out
            assert fetch(manager.tester_container, "allowed.test").exit_code != 0
            # Local services remain available under the restricted policy.
            check = "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); socket.create_connection(s.getsockname(),2)"
            assert client.exec_run(["python3", "-c", check]).exit_code == 0
        assert fetch(client, "blocked.test").exit_code != 0
        with manager.agent_network_phase(uses_host_gateway=True):
            effective = json.loads((tmp_path / "logs/network-policy.json").read_text())
            controller = manager._service_container(manager._network_controllers["client"])
            gateway = next(iter(controller.attrs["NetworkSettings"]["Networks"].values()))["Gateway"]
            assert gateway in effective["agent"]["allowed_hosts"]
            assert "fake-model-key" not in json.dumps(effective)
            assert fetch(client, "blocked.test").exit_code != 0
        manager.set_network_policy("client", NetworkPolicy(network_mode="no-network"))
        assert fetch(client, "allowed.test").exit_code != 0
        assert compose.read_bytes() == original
        assert (tmp_path / "logs/network-policy.json").is_file()
    finally:
        if live is not None and live.poll() is None:
            live.kill()
            live.communicate()
        manager.stop()
