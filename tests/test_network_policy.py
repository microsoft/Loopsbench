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
    NetworkPlan,
    NetworkPolicy,
    controlled_compose,
)


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


def test_custom_agent_uses_prepare_then_scoped_solve_even_on_failure(tmp_path):
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
    def phase():
        events.append("allowlist")
        try:
            yield
        finally:
            events.append("restore")

    harness = Harness.__new__(Harness)
    harness._logger = logging.getLogger(__name__)
    result, failure = harness._run_agent(
        SimpleNamespace(agent_network_phase=phase), SimpleNamespace(task_id="test"), CustomAgent(), "fix",
        timeout_sec=10, logging_dir=tmp_path,
    )
    assert events == ["prepare", "allowlist", "solve", "restore"]
    assert result is None and failure == FailureMode.UNKNOWN_AGENT_ERROR

    class LegacyAgent(BaseAgent):
        @staticmethod
        def name():
            return "legacy"

        def perform_task(self, **kwargs):
            return AgentResult()

    # Old plugins implement only perform_task; no new abstract method is needed.
    assert LegacyAgent().prepare(None) is None


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
               "network_mode": "none", "working_dir": "/workspace", "volumes": ["./legacy-data:/legacy:ro"]}
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
        network=NetworkPlan(agent=NetworkPolicy(network_mode="allowlist", allowed_hosts=["allowed.test"])),
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
        assert fetch(client, "blocked.test").exit_code == 0
        manager.set_network_policy("client", NetworkPolicy(network_mode="no-network"))
        assert fetch(client, "allowed.test").exit_code != 0
        assert compose.read_bytes() == original
        assert (tmp_path / "logs/network-policy.json").is_file()
    finally:
        if live is not None and live.poll() is None:
            live.kill()
            live.communicate()
        manager.stop()
