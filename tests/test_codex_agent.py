from __future__ import annotations

import io
import os
import tarfile
import tomllib
from types import SimpleNamespace

import pytest

from loopsbench.agents.codex import codex_agent
from loopsbench.agents.codex.codex_agent import CodexAgent
from loopsbench.terminal.network_policy import MODEL_URL_ENV_KEYS


@pytest.fixture(autouse=True)
def model_environment(monkeypatch):
    for key in [*MODEL_URL_ENV_KEYS, "OPENAI_API_KEY", "PROXY_TOK", "ANTHROPIC_AUTH_TOKEN", "CODEX_BIN_HOST"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")


def provider(agent):
    return tomllib.loads(agent._config_toml())["model_providers"]["loopsbench"]


@pytest.mark.parametrize("model", ["gpt-example", "openai/gpt-example"])
def test_default_openai_endpoint_and_bare_model(model):
    agent = CodexAgent(model)
    config = tomllib.loads(agent._config_toml())
    assert config["model"] == "gpt-example"
    assert config["web_search"] == "disabled"
    assert provider(agent)["base_url"] == "https://api.openai.com/v1"
    assert provider(agent)["wire_api"] == "responses"
    assert "api.openai.com" in agent.model_network_hosts()
    assert not agent.model_uses_host_gateway


@pytest.mark.parametrize("name", ["OPENAI_BASE_URL", "OPENAI_API_BASE"])
def test_custom_environment_url_preserves_path_port_and_model(name, monkeypatch):
    monkeypatch.setenv(name, "https://models.example.test:8443/team/responses-api/v1/")
    agent = CodexAgent("custom/owner/model-version")
    assert tomllib.loads(agent._config_toml())["model"] == "owner/model-version"
    assert provider(agent)["base_url"] == "https://models.example.test:8443/team/responses-api/v1"
    assert "models.example.test" in agent.model_network_hosts()
    assert agent._api_key == "test-openai-key"
    assert not agent.model_uses_host_gateway


def test_explicit_url_key_and_key_environment_precedence(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://default.example/v1")
    monkeypatch.setenv("CUSTOM_KEY", "custom-env-secret")
    agent = CodexAgent("custom/model", base_url="https://chosen.example/v2", api_key_env="CUSTOM_KEY")
    assert provider(agent)["base_url"] == "https://chosen.example/v2"
    assert agent._api_key == "custom-env-secret"
    assert CodexAgent("model", api_key="explicit-secret", api_key_env="CUSTOM_KEY")._api_key == "explicit-secret"


def test_azure_uses_explicit_responses_endpoint_and_query_parameters(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-secret")
    agent = CodexAgent(
        "azure/my-deployment", base_url="https://resource.openai.azure.com/openai",
        api_key_env="AZURE_OPENAI_API_KEY", query_params={"api-version": "2025-04-01-preview"},
    )
    assert provider(agent)["query_params"] == {"api-version": "2025-04-01-preview"}
    assert tomllib.loads(agent._config_toml())["model"] == "my-deployment"
    assert agent._api_key == "azure-secret"


def test_toml_escapes_model_and_provider_query_values():
    agent = CodexAgent('custom/quoted"model\\name\n🦀', base_url="https://models.example/v1", query_params={'quoted"key': 'a"b\\c'})
    parsed = tomllib.loads(agent._config_toml())
    assert parsed["model"] == 'quoted"model\\name\n🦀'
    assert provider(agent)["query_params"] == {'quoted"key': 'a"b\\c'}


def test_legacy_copilot_proxy_auth_and_default_route(monkeypatch):
    monkeypatch.setenv("PROXY_TOK", "proxy-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "legacy-secret")
    agent = CodexAgent("copilot/model")
    assert agent._api_key == "legacy-secret"
    assert provider(agent)["base_url"] == "http://localhost:23335/api/openai/v1"
    assert agent.model_uses_host_gateway


def test_remote_proxy_is_not_rewritten_to_docker_host(monkeypatch):
    monkeypatch.setenv("PROXY_TOK", "proxy-secret")
    monkeypatch.setenv("PROXY_BASE", "https://maestro.example:8443")
    agent = CodexAgent("copilot/model")
    assert provider(agent)["base_url"] == "https://maestro.example:8443/api/openai/v1"
    assert "maestro.example" in agent.model_network_hosts()
    assert not agent.model_uses_host_gateway
    direct = CodexAgent("copilot/model", base_url="https://direct.example/custom/v1")
    assert direct._api_key == "test-openai-key"
    assert provider(direct)["base_url"] == "https://direct.example/custom/v1"


@pytest.mark.parametrize("url", ["file:///tmp/api", "https://user:secret@example.org/v1", "https://example.org/v1#fragment", "https://example.org/v1/responses", "https://example.org/v1/chat/completions", "   "])
def test_invalid_api_urls_fail_without_echoing_credentials(url):
    with pytest.raises(ValueError) as error:
        CodexAgent("model", base_url=url)
    assert "secret" not in str(error.value)


def test_missing_credentials_and_unknown_provider_fail_early(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(ValueError, match="API key"):
        CodexAgent("model")
    with pytest.raises(ValueError, match="base URL"):
        CodexAgent("custom/model")


class Container:
    def __init__(self, gateway="172.21.0.1"):
        self.attrs = {"NetworkSettings": {"Networks": {"default": {"Gateway": gateway}}}}
        self.files = {}
        self.commands = []

    def exec_run(self, command):
        self.commands.append(command)
        return SimpleNamespace(exit_code=0)

    def put_archive(self, directory, payload):
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            for member in archive:
                assert member.mode == 0o600
                self.files[f"{directory}/{member.name}"] = archive.extractfile(member).read().decode()
        return True


@pytest.mark.parametrize("url", ["http://localhost:9876/team/v1", "http://127.0.0.1:9876/team/v1", "http://[::1]:9876/team/v1"])
def test_preparation_uses_trusted_gateway_and_keeps_secret_out_of_recorded_commands(url):
    agent = CodexAgent("custom/model", base_url=url, api_key='key-$(must-not-run)-"secret')
    client, controller = Container("198.51.100.99"), Container()
    manager = SimpleNamespace(_network_controllers={"client": "egress"}, _service_container=lambda _: controller)
    commands = []
    session = SimpleNamespace(_container=client, _network_controller=manager, send_keys=lambda command, **kw: commands.append(command))
    agent.prepare(session)
    config = tomllib.loads(client.files["/root/.codex/config.toml"])
    assert config["model_providers"]["loopsbench"]["base_url"] == "http://172.21.0.1:9876/team/v1"
    assert client.files["/run/loopsbench-codex/api-key"] == agent._api_key
    assert agent._api_key not in str(commands) + str(client.commands) + client.files["/root/.codex/config.toml"]
    assert all(not path.startswith("/run/loopsbench-codex") for path in agent.get_trajectory_paths())
    assert agent.model_uses_host_gateway


def test_remote_preparation_preserves_url_and_rejects_failed_transfer():
    agent = CodexAgent("model", base_url="https://models.example:8443/team/v1")
    client = Container()
    session = SimpleNamespace(_container=client, send_keys=lambda *a, **kw: None)
    agent.prepare(session)
    config = tomllib.loads(client.files["/root/.codex/config.toml"])
    assert config["model_providers"]["loopsbench"]["base_url"] == agent._base_url
    client.put_archive = lambda *a: False
    with pytest.raises(RuntimeError, match="runtime configuration"):
        agent.prepare(session)


def test_binary_selection_explicit_path_and_npm_native_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_agent, "_CODEX_BIN_HOST", tmp_path / "absent")
    monkeypatch.setattr(codex_agent.platform, "machine", lambda: "x86_64")
    package = tmp_path / "node_modules/@openai/codex"
    launcher = package / "bin/codex.js"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/usr/bin/env node")
    native = package / "node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"\x7fELFstub")
    monkeypatch.setattr(codex_agent.shutil, "which", lambda _: str(launcher))
    assert CodexAgent("model")._resolve_binary() == native
    explicit = tmp_path / "native-codex"
    explicit.write_bytes(b"\x7fELFstub")
    assert CodexAgent("model", codex_binary=str(explicit))._resolve_binary() == explicit
    with pytest.raises(RuntimeError, match="Configured Codex binary"):
        CodexAgent("model", codex_binary=str(tmp_path / "missing"))._resolve_binary()


def test_perform_task_retains_search_restriction_and_obeys_timeout():
    commands = []
    session = SimpleNamespace(send_keys=lambda command, **kw: commands.append((command, kw)))
    CodexAgent("model").perform_task("finish the task", session, timeout_sec=23)
    assert 'web_search="disabled"' in commands[0][0]
    assert commands[0][1]["max_timeout_sec"] == 23


@pytest.mark.skipif(os.environ.get("LOOPSBENCH_TEST_CODEX_DOCKER") != "1", reason="opt-in native Codex/Docker integration")
def test_native_codex_sends_configured_model_path_and_credentials_across_rounds(tmp_path):
    import json
    import threading
    import uuid
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from loopsbench.terminal.docker_compose_manager import DockerComposeManager
    from loopsbench.terminal.network_policy import NetworkPlan
    from loopsbench.terminal.tmux_session import TmuxSession

    requests = []

    class Responses(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, payload["model"], self.headers.get("Authorization")))
            text = f"ADAPTER_OK_{len(requests)}"
            item = {"id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}
            response = {"id": "resp_test", "object": "response", "created_at": 1, "model": payload["model"],
                        "status": "completed", "output": [item],
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
            events = [
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0, "item": {**item, "status": "in_progress", "content": []}},
                {"type": "response.content_part.added", "item_id": "msg_test", "output_index": 0, "content_index": 0,
                 "part": {"type": "output_text", "text": "", "annotations": []}},
                {"type": "response.output_text.delta", "item_id": "msg_test", "output_index": 0, "content_index": 0, "delta": text},
                {"type": "response.output_text.done", "item_id": "msg_test", "output_index": 0, "content_index": 0, "text": text},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]
            body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("0.0.0.0", 0), Responses)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    project = "lb-codex-" + uuid.uuid4().hex[:10]
    image = "python:3.12-slim"
    compose = tmp_path / "compose.json"
    common = {"image": image, "working_dir": "/workspace", "command": ["sleep", "infinity"]}
    compose.write_text(json.dumps({"services": {
        "client": {**common, "container_name": project + "-client"},
        "tester": {**common, "container_name": project + "-tester"},
    }}))
    manager = DockerComposeManager(
        client_container_name=project + "-client", client_image_name=image,
        tester_container_name=project + "-tester", tester_image_name=image,
        docker_compose_path=compose, no_rebuild=True, logs_path=tmp_path / "logs", network=NetworkPlan(),
    )
    try:
        client = manager.start()
        install = client.exec_run(["bash", "-lc", "apt-get update -qq && apt-get install -y -qq tmux"])
        assert install.exit_code == 0, install.output.decode()
        for round_number in (1, 2):
            session = TmuxSession(client, f"agent-{round_number}", commands_path=tmp_path / "commands.txt", network_controller=manager)
            host = "localhost" if round_number == 1 else CodexAgent._gateway(session)
            agent = CodexAgent(f"custom/model-{round_number}", base_url=f"http://{host}:{server.server_port}/team-{round_number}/v1", api_key=f"test-key-{round_number}")
            session.prepare_agent(agent, tmp_path)
            client.exec_run(["rm", "-f", codex_agent._LAST_MSG_CONTAINER])
            with session.agent_network_phase(model_hosts=agent.model_network_hosts(), uses_host_gateway=agent.model_uses_host_gateway):
                agent.perform_task("Reply with one word.", session, logging_dir=tmp_path, timeout_sec=45)
            result = client.exec_run(["cat", codex_agent._LAST_MSG_CONTAINER])
            log = client.exec_run(["cat", codex_agent._TRAJECTORY_CONTAINER]).output.decode()
            assert result.exit_code == 0 and result.output.decode().strip() == f"ADAPTER_OK_{round_number}", log[-4000:]
        assert requests == [("/team-1/v1/responses", "model-1", "Bearer test-key-1"),
                            ("/team-2/v1/responses", "model-2", "Bearer test-key-2")]
        assert len(manager._agent_installations) == 1
        assert "test-key-" not in (tmp_path / "commands.txt").read_text()
    finally:
        manager.stop()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
