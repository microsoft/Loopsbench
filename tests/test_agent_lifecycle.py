"""Installation reuse and persistent network restrictions across agent rounds."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import uuid
from types import SimpleNamespace

import pytest

from loopsbench.agents.base_agent import AgentResult, BaseAgent
from loopsbench.harness.harness import Harness
from loopsbench.harness.models import FailureMode
from loopsbench.terminal.docker_compose_manager import DockerComposeManager
from loopsbench.terminal.network_policy import NetworkPlan, NetworkPolicy
from loopsbench.terminal.tmux_session import TmuxSession


def controller(plan=None):
    manager = DockerComposeManager.__new__(DockerComposeManager)
    manager._network = plan or NetworkPlan()
    manager._network_controllers = {'client': 'egress-client', 'tester': 'egress-tester'}
    manager._agent_network_started = False
    manager._agent_installations = {}
    manager.policy_calls = []
    manager.phase_calls = []
    manager._run_compose = manager.policy_calls.append
    manager._record_network_policy = lambda policy: None
    manager.dependencies = SimpleNamespace(client_phase=manager.phase_calls.append)
    return manager


def session(manager, name='round-1', container_id='client'):
    value = TmuxSession.__new__(TmuxSession)
    value._container = SimpleNamespace(id=container_id)
    value._network_controller = manager
    value._session_name = name
    return value


def harness():
    value = Harness.__new__(Harness)
    value._logger = logging.getLogger(__name__)
    value._rate_limit_retries_per_round = 1
    value._rate_limit_backoff_sec = 1
    return value


class Plugin(BaseAgent):
    @staticmethod
    def name():
        return 'custom'

    def perform_task(self, **kwargs):
        return AgentResult()


@pytest.mark.parametrize('error', [None, RuntimeError('failed'), TimeoutError('timed out')])
def test_network_and_dependency_policy_survive_exit_and_nested_invocations(error):
    manager = controller()
    try:
        with manager.agent_network_phase(model_hosts=['model.example.test']):
            with manager.agent_network_phase(model_hosts=['model.example.test']):
                pass
            assert manager.phase_calls == [True, True]
            if error:
                raise error
    except (RuntimeError, TimeoutError):
        assert error is not None
    assert manager.phase_calls == [True, True]
    assert all(call[3:] == ['network-policy', 'allow', 'model.example.test'] for call in manager.policy_calls)
    with pytest.raises(ValueError, match='restore public'):
        manager.set_network_policy('client', NetworkPolicy())
    # The independent tester policy remains configurable.
    manager.set_network_policy('tester', NetworkPolicy(network_mode='no-network'))


def test_explicit_public_policy_stays_supported():
    manager = controller(NetworkPlan(agent={'network_mode': 'public'}))
    with manager.agent_network_phase():
        pass
    assert manager.policy_calls[-1][3:] == ['network-policy', 'allow-all']


def test_setup_failure_never_restores_public_access():
    manager = controller()
    manager.dependencies.client_phase = lambda _: (_ for _ in ()).throw(RuntimeError('setup failed'))
    with pytest.raises(RuntimeError, match='setup failed'):
        with manager.agent_network_phase():
            pytest.fail('must not start solving')
    assert manager.policy_calls == [['exec', '-T', 'egress-client', 'network-policy', 'allow']]


def test_new_agent_objects_and_sessions_reuse_host_installation_but_initialize_each_time(tmp_path):
    manager = controller()
    events = []

    class InstalledPlugin(Plugin):
        def install(self, session, logging_dir=None):
            events.append(('install', session._container.id))
            return ('/opt/agent/bin', 'setup-state')

        def prepare(self, session, logging_dir=None):
            assert self.installation == ('/opt/agent/bin', 'setup-state')
            events.append(('prepare', session._session_name))

    for name in ('round-1', 'round-2'):
        current = session(manager, name)
        agent = InstalledPlugin()
        current.prepare_agent(agent, tmp_path)
        with current.agent_network_phase():
            agent.perform_task()
    assert events == [('install', 'client'), ('prepare', 'round-1'), ('prepare', 'round-2')]
    session(manager, 'new-container', 'another-client').prepare_agent(InstalledPlugin(), tmp_path)
    assert events[-2:] == [('install', 'another-client'), ('prepare', 'new-container')]


def test_failed_install_not_cached_and_legacy_plugins_without_super_keep_prepare(tmp_path):
    manager = controller()
    attempts = []

    class Legacy(Plugin):
        def __init__(self):
            pass

        def install(self, session, logging_dir=None):
            attempts.append('install')
            if attempts.count('install') == 1:
                raise RuntimeError('installation failed')

        def prepare(self, session, logging_dir=None):
            attempts.append('prepare')

    current = session(manager)
    with pytest.raises(RuntimeError, match='installation failed'):
        current.prepare_agent(Legacy(), tmp_path)
    current.prepare_agent(Legacy(), tmp_path)
    with current.agent_network_phase():
        pass
    current.prepare_agent(Legacy(), tmp_path)
    assert attempts == ['install', 'install', 'prepare', 'prepare']


def test_rate_limit_retry_and_async_entry_keep_preparation_restricted(tmp_path, monkeypatch):
    manager = controller()
    events = []

    class Retrying(Plugin):
        def install(self, **kwargs):
            events.append('install')

        def prepare(self, **kwargs):
            events.append(('prepare', manager._agent_network_started))

        def perform_task(self, **kwargs):
            events.append('solve')
            return AgentResult(failure_mode='agent_rate_limited' if events.count('solve') == 1 else 'none')

    def backoff(seconds):
        assert seconds == 1
        assert manager._agent_network_started and manager.phase_calls[-1] is True
        assert all('allow-all' not in call for call in manager.policy_calls)
        events.append('backoff')

    monkeypatch.setattr('loopsbench.harness.harness.time.sleep', backoff)
    runner = harness()
    agent = Retrying()
    result, failure = runner._run_agent_with_rate_limit_retries(
        session(manager), SimpleNamespace(task_id='old-task'), agent, 'fix', logging_dir=tmp_path, timeout_sec=10,
    )
    assert result and failure == FailureMode.NONE
    result = asyncio.run(runner._run_agent_with_timeout(session(manager, 'next-round'), tmp_path, 10, Retrying(), 'fix'))
    assert result.failure_mode == 'none'
    assert events == ['install', ('prepare', False), 'solve', 'backoff', ('prepare', True), 'solve', ('prepare', True), 'solve']


@pytest.mark.parametrize('kind', ['codex', 'mini', 'openhands', 'swe', 'qwen', 'copilot'])
def test_builtin_agents_reuse_installation_and_restore_session_configuration(kind, tmp_path, monkeypatch):
    from loopsbench.agents.codex import codex_agent
    from loopsbench.agents.copilot.copilot_agent import CopilotAgent
    from loopsbench.agents.mini_swe_agent.mini_swe_agent import MiniSweAgent
    from loopsbench.agents.openhands.openhands_agent import OpenHandsAgent
    from loopsbench.agents.qwen_code import qwen_code_agent
    from loopsbench.agents.swe_agent.swe_agent import SweAgent

    cls = {'codex': codex_agent.CodexAgent, 'mini': MiniSweAgent, 'openhands': OpenHandsAgent,
           'swe': SweAgent, 'qwen': qwen_code_agent.QwenCodeAgent, 'copilot': CopilotAgent}[kind]
    actions = []
    manager = controller()
    monkeypatch.delenv('LOOPSBENCH_MINI_BUNDLE', raising=False)
    if hasattr(cls, '_env_exports'):
        monkeypatch.setattr(cls, '_env_exports', property(lambda self: ['export MODEL_KEY=test']))
    monkeypatch.setattr(DockerComposeManager, 'copy_to_container', lambda **kwargs: actions.append('copy'))
    executable = tmp_path / 'codex'
    executable.touch()
    monkeypatch.setattr(codex_agent, '_CODEX_BIN_HOST', executable)
    monkeypatch.setattr(codex_agent.CodexAgent, '_materialize_config', lambda *a: actions.append('config'))
    for copied in (OpenHandsAgent, SweAgent):
        monkeypatch.setattr(copied, '_copy_src_to_container', lambda *a: actions.append('copy'))
    monkeypatch.setattr(CopilotAgent, '_ensure_copilot_cli', lambda *a: actions.append('copilot-install'))
    monkeypatch.setattr(qwen_code_agent, '_workspace_mode', lambda: 'container')

    def qwen_install(*args):
        actions.append('qwen-install')
        return '/opt/qwen', '/opt/node/bin'

    monkeypatch.setattr(qwen_code_agent, '_ensure_container_qwen_cli', qwen_install)
    for index in range(2):
        current = session(manager, f'round-{index}')
        current._container.exec_run = lambda *a, **k: actions.append('exec')
        current.send_keys = lambda command, **kwargs: actions.append(command)
        agent = cls.__new__(cls)
        start = len(actions)
        current.prepare_agent(agent, tmp_path)
        if index == 0:
            with current.agent_network_phase():
                pass
        else:
            later = actions[start:]
            assert not any('install' in command or 'curl ' in command or command == 'copy' for command in later)
            if kind in ('codex', 'mini', 'openhands', 'swe'):
                assert 'export MODEL_KEY=test Enter' in later
            if kind in ('openhands', 'swe'):
                assert any('/bin/activate' in command for command in later)
            if kind == 'codex':
                assert 'config' in later
            if kind == 'qwen':
                assert agent.installation == ('/opt/qwen', '/opt/node/bin')


@pytest.mark.skipif(os.environ.get('LOOPSBENCH_TEST_NETWORK_DOCKER') != '1', reason='opt-in Docker integration')
def test_real_background_process_cannot_fetch_after_return_retry_or_failure(tmp_path):
    image = os.environ.get('LOOPSBENCH_TEST_NETWORK_IMAGE', 'python:3.12-slim')
    project = 'lb-lifecycle-' + uuid.uuid4().hex[:10]
    server = 'from http.server import HTTPServer,SimpleHTTPRequestHandler; HTTPServer(("0.0.0.0",8765),SimpleHTTPRequestHandler).serve_forever()'
    compose = tmp_path / 'compose.json'
    common = {'image': image, 'command': ['sh', '-c', 'mkdir -p /workspace; exec sleep infinity']}
    compose.write_text(json.dumps({'services': {
        'client': {**common, 'container_name': project + '-client'},
        'tester': {**common, 'container_name': project + '-tester'},
        'upstream': {'image': image, 'command': ['python3', '-c', server],
                     'networks': {'default': {'aliases': ['allowed.test', 'blocked.test']}}},
    }}))
    original = compose.read_bytes()
    manager = DockerComposeManager(
        client_container_name=project + '-client', client_image_name=image,
        tester_container_name=project + '-tester', tester_image_name=image,
        docker_compose_path=compose, no_rebuild=True, logs_path=tmp_path / 'logs',
        network=NetworkPlan(extra_allowed_hosts=['allowed.test']),
    )
    probe = '''import socket
host=HOST
s=socket.create_connection((host,8765),2);s.settimeout(2)
s.sendall(('GET / HTTP/1.0\\r\\nHost: '+host+'\\r\\n\\r\\n').encode())
assert b'200' in s.recv(1024)
'''
    pending = None
    try:
        client = manager.start()
        upstream = manager._service_container('upstream')
        address = next(iter(upstream.attrs['NetworkSettings']['Networks'].values()))['IPAddress']

        def fetch(container, host):
            return container.exec_run(['python3', '-c', probe.replace('HOST', repr(host))]).exit_code == 0

        assert fetch(client, 'blocked.test')
        assert not fetch(manager.tester_container, 'allowed.test')
        with manager.agent_network_phase():
            # This process starts during solving, then waits until the caller
            # has returned. Use the known IP so denied DNS alone cannot pass.
            script = 'print("ready",flush=True); input();\n' + probe.replace('HOST', repr(address))
            pending = subprocess.Popen(['docker', 'exec', '-i', client.id, 'python3', '-c', script],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert pending.stdout.readline().strip() == 'ready'
            assert fetch(client, 'allowed.test')
        _, _ = pending.communicate('after return\n', timeout=10)
        assert pending.returncode != 0
        assert not fetch(client, 'blocked.test')
        assert fetch(client, 'allowed.test')
        with pytest.raises(TimeoutError):
            with manager.agent_network_phase():
                assert fetch(client, 'allowed.test')
                raise TimeoutError('round timed out')
        assert not fetch(client, address)
        assert not fetch(manager.tester_container, 'allowed.test')
        with pytest.raises(ValueError, match='restore public'):
            manager.set_network_policy('client', NetworkPolicy())
        # Collecting files and creating subsequent invocations still work.
        assert client.exec_run(['cat', '/etc/os-release']).exit_code == 0
        with manager.agent_network_phase():
            assert fetch(client, 'allowed.test')
        assert compose.read_bytes() == original
    finally:
        if pending is not None and pending.poll() is None:
            pending.kill()
            pending.communicate()
        manager.stop()
