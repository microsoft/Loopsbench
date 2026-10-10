"""Opt-in package-manager tests through the real Compose firewall/gateway."""
from __future__ import annotations

import json
import os
import uuid

import pytest

from loopsbench.dependencies.config import DependencyAccess, DependencyService
from loopsbench.harness.test_runner import TestRunner as Runner
from loopsbench.terminal.docker_compose_manager import DockerComposeManager
from loopsbench.terminal.network_policy import NetworkPlan

pytestmark = pytest.mark.skipif(os.environ.get('LOOPSBENCH_TEST_DEPENDENCIES_DOCKER') != '1', reason='opt-in Docker dependency integration')


@pytest.mark.parametrize('language', ['python', 'go'])
def test_install_new_version_test_snapshot_rollback_and_network_boundary(tmp_path, language):
    image = os.environ.get('LOOPSBENCH_TEST_DEPENDENCIES_' + language.upper() + '_IMAGE',
                           'python:3.12-slim' if language == 'python' else 'golang:1.26.5-bookworm')
    project = 'lb-dep-' + language + '-' + uuid.uuid4().hex[:10]
    logs = tmp_path / 'logs'
    snapshots = tmp_path / 'snapshots'
    snapshots.mkdir()
    versions = ['8.1.7', '8.1.8'] if language == 'python' else ['v1.5.0', 'v1.6.0']
    for version in versions:
        snapshot = snapshots / version
        snapshot.mkdir()
        (snapshot / 'expected').write_text(version)
        if language == 'python':
            (snapshot / 'requirements.txt').write_text('click==' + version + '\n')
        else:
            (snapshot / 'go.mod').write_text('module example.com/candidate\n\ngo 1.20\n\nrequire github.com/google/uuid ' + version + '\n')
            (snapshot / 'main.go').write_text('package candidate\nimport "github.com/google/uuid"\nfunc UUID() string { return uuid.NewString() }\n')
    script = tmp_path / 'run-tests.sh'
    if language == 'python':
        script.write_text("set -e\npython3 -c \"import importlib.metadata as m; assert m.version('click') == open('expected').read()\"\necho 'tests/test_dependency.py::version PASSED'\n")
        policy = DependencyAccess(ecosystems=['python'], blocked_packages={'python': ['packaging']},
                                  python={'project': None, 'requirements_files': ['requirements.txt']})
    else:
        script.write_text("set -e\ngo test -mod=mod ./...\ntest \"$(go list -m -f '{{.Version}}' github.com/google/uuid)\" = \"$(cat expected)\"\necho 'tests/test_dependency.py::version PASSED'\n")
        policy = DependencyAccess(ecosystems=['go'], blocked_packages={'go': ['github.com/expr-lang/expr']})
    common = {'image': image, 'entrypoint': ['sh', '-c'], 'command': ['mkdir -p /workspace; exec sleep infinity'],
              'working_dir': '/workspace'}
    compose = tmp_path / 'compose.json'
    compose.write_text(json.dumps({'services': {
        'client': {**common, 'container_name': project + '-client', 'networks': {'task-private': {}},
                   'volumes': ['${LOOPSBENCH_TASK_LOGS_PATH}:/logs']},
        'tester': {**common, 'container_name': project + '-tester'},
    }, 'networks': {'task-private': {}}}))
    overlay = tmp_path / 'harness.compose.json'
    overlay.write_text(json.dumps({'services': {'tester': {
        'volumes': [f'{snapshots}:/snapshots:ro', f'{script}:/task-assets/run-tests.sh:ro'],
    }}}))
    manager = DockerComposeManager(client_container_name=project + '-client', client_image_name=image,
                                    tester_container_name=project + '-tester', tester_image_name=image,
                                    docker_compose_path=[compose, overlay], no_rebuild=True, logs_path=logs,
                                    network=NetworkPlan(), dependency_access=policy,
                                    dependency_service=DependencyService())
    try:
        client = manager.start()
        runner = Runner(project + '-tester', dependencies=manager.dependencies)
        if language == 'python':
            result = client.exec_run(['python3', '-m', 'pip', 'index', 'versions', 'packaging'])
            assert result.exit_code == 0, result.output.decode()  # Agent installation retains the public preparation policy.
        else:
            assert b'loopsbench-dependencies' not in client.exec_run(['go', 'env', 'GOPROXY']).output
        # Both versions begin absent from the gateway cache. Client and tester
        # have separate installed state and share only the service download data.
        with manager.agent_network_phase():
            if language == 'python':
                assert client.exec_run(['python3', '-m', 'pip', 'index', 'versions', 'packaging']).exit_code != 0
            else:
                assert b'loopsbench-dependencies' in client.exec_run(['go', 'env', 'GOPROXY']).output
            for version in versions:
                if language == 'python':
                    result = client.exec_run(['python3', '-m', 'pip', 'install', 'click==' + version])
                    assert result.exit_code == 0, result.output.decode()
                else:
                    result = client.exec_run(['go', 'mod', 'download', 'github.com/google/uuid@' + version])
                    assert result.exit_code == 0, result.output.decode()
            for container in [client, manager.tester_container]:
                for url in ['https://github.com/expr-lang/expr', 'https://pypi.org/simple/click/', 'https://proxy.golang.org/github.com/google/uuid/@v/list']:
                    probe = "import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open(" + repr(url) + ",timeout=3)"
                    assert container.exec_run(['python3', '-c', probe]).exit_code != 0
                blocked = '/python/simple/packaging/' if language == 'python' else '/go/github.com/expr-lang/expr/@v/list'
                probe = "import urllib.request,urllib.error\ntry: urllib.request.urlopen('http://loopsbench-dependencies:8080/tester" + blocked + "')\nexcept urllib.error.HTTPError as e: assert e.code==403\nelse: raise AssertionError('target allowed')"
                assert container.exec_run(['python3', '-c', probe]).exit_code == 0
            for version in [*versions, versions[0]]:
                result = runner.run('/snapshots/' + version, timeout_sec=300)
                assert not result.infrastructure_error, result
                assert result.exit_code == 0, result.raw_stdout + result.raw_stderr
                assert result.passed == ['tests/test_dependency.py::version']
        if language == 'python':
            assert client.exec_run(['python3', '-m', 'pip', 'index', 'versions', 'packaging']).exit_code != 0
            result = client.exec_run(['python3', '-m', 'pip', 'install', 'click==' + versions[0]])
            assert result.exit_code == 0, result.output.decode()
        else:
            assert b'loopsbench-dependencies' in client.exec_run(['go', 'env', 'GOPROXY']).output
        assert manager.dependencies.phase_path.read_text() == 'solving'
        resolution = json.loads((manager.dependencies.directory / 'data/resolution.json').read_text())
        assert any('sha256' in record for record in resolution.values())
        assert len(list(manager.dependencies.directory.glob('prepare-*.log'))) == 3
        # Read-only trusted config and cache isolation apply to both containers.
        for container in [client, manager.tester_container]:
            destinations = {mount['Destination'] for mount in container.attrs['Mounts']}
            assert '/data' not in destinations
            assert '/config.json' not in destinations
            assert container.exec_run(['sh', '-c', 'echo tampered > /opt/loopsbench-dependency-config.json']).exit_code != 0
            assert container.exec_run(['sh', '-c', 'test ! -e /logs/dependencies/data && test ! -e /logs/dependencies/gateway.json']).exit_code == 0
    finally:
        manager.stop()
