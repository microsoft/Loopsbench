from __future__ import annotations

import hashlib
import io
import json
import threading
import zipfile
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from loopsbench.dependencies.config import (
    DependencyAccess,
    DependencyService,
    dependency_network,
)
from loopsbench.dependencies.protocol import check_package
from loopsbench.dependencies.runtime import DependencyInfrastructureError
from loopsbench.dependencies.server import Gateway, GatewayError, make_server
from loopsbench.file_config import load_run_file
from loopsbench.harness.test_runner import TestRunner as Runner
from loopsbench.terminal.network_policy import NetworkPlan


def access(**kwargs):
    return DependencyAccess(ecosystems=['python', 'go'], blocked_packages={
        'python': ['target_project'], 'go': ['github.com/owner/target'],
    }, **kwargs)


@contextmanager
def serving(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def registry():
    data = {}
    counts = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            counts[self.path] = counts.get(self.path, 0) + 1
            status, headers, body = data.get(self.path, (404, {}, b'missing'))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    with serving(ThreadingHTTPServer(('127.0.0.1', 0), Handler)) as endpoint:
        yield endpoint, data, counts


def gateway(tmp_path, registry, policy=None, **options):
    endpoint, _, _ = registry
    service = DependencyService(
        python_metadata_url=endpoint + '/pypi', python_artifact_hosts=['127.0.0.1'],
        go_proxy_url=endpoint + '/go', go_artifact_hosts=['127.0.0.1'],
        go_sumdb_url=endpoint + '/sumdb', allow_insecure_upstreams=True, **options,
    )
    return Gateway({'policy': (policy or access()).wire_policy(), 'service': service.model_dump()}, tmp_path / 'data')


def wheel(name, version):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr(zipfile.ZipInfo(f'{name}/__init__.py'), f'__version__ = {version!r}\n')
        prefix = f'{name}-{version}.dist-info/'
        archive.writestr(zipfile.ZipInfo(prefix + 'METADATA'), f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n')
        archive.writestr(zipfile.ZipInfo(prefix + 'WHEEL'), 'Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        archive.writestr(zipfile.ZipInfo(prefix + 'RECORD'), '')
    return stream.getvalue()


def add_python(registry, name='sample', version='1.0'):
    endpoint, data, _ = registry
    blob = wheel(name, version)
    filename = f'{name}-{version}-py3-none-any.whl'
    item = {'filename': filename, 'url': endpoint + '/files/' + filename,
            'digests': {'sha256': hashlib.sha256(blob).hexdigest()}}
    data['/files/' + filename] = 200, {}, blob
    data[f'/pypi/{name}/json'] = 200, {}, json.dumps({'info': {'name': name}, 'releases': {version: [item]}}).encode()
    return item


def test_configuration_keeps_explicit_offline_distinct_from_default(tmp_path):
    path = tmp_path / 'run.yaml'
    path.write_text('task_dependency_overrides:\n  old_task:\n    ecosystems: [go]\n    blocked_packages:\n      go: [github.com/owner/target]\n')
    run = load_run_file(path)
    assert run.task_dependency_overrides['old_task'].ecosystems == ['go']
    plan = dependency_network(NetworkPlan.from_config(None), access(), 'http://gateway:8080')
    assert plan.verifier.allowed_hosts == ['gateway']
    for phase in ['agent', 'verifier']:
        with pytest.raises(ValueError, match='conflicts'):
            dependency_network(NetworkPlan.model_validate({phase: {'network_mode': 'no-network'}}), access(), 'http://gateway:8080')
    assert NetworkPlan().verifier.network_mode == 'no-network'
    with pytest.raises(ValueError, match='blocked_packages'):
        DependencyAccess(ecosystems=['go'])
    with pytest.raises(ValueError):
        DependencyAccess(ecosystems=['rust'], blocked_packages={'rust': ['target']})


@pytest.mark.parametrize('name', ['target-project', 'TARGET.Project', 'target___project'])
def test_python_alias_normalization_blocks_target(name):
    with pytest.raises(PermissionError):
        check_package(access().wire_policy(), 'python', name)


@pytest.mark.parametrize('name', ['github.com/owner/target', 'github.com/owner/target/v2', 'golang.org/toolchain',
                                  'github.com/!owner/!target/v2', 'github.com/owner/target.git'])
def test_go_target_and_major_versions_blocked(name):
    with pytest.raises(PermissionError):
        check_package(access().wire_policy(), 'go', name)


def test_policy_checked_before_shared_cache(tmp_path, registry):
    item = add_python(registry)
    g = gateway(tmp_path, registry)
    body, _ = g.route('/client/python/simple/sample/')
    assert b'/client/python/files/sample/' in body
    route = f'/tester/python/files/sample/{item["digests"]["sha256"]}/{item["filename"]}'
    assert g.route(route)[0] == wheel('sample', '1.0')
    # Same cache under a more restrictive task must never serve old records.
    stricter = DependencyAccess(ecosystems=['python'], blocked_packages={'python': ['sample']})
    g2 = gateway(tmp_path, registry, stricter)
    for path in ['/client/python/simple/SAMPLE/', route]:
        with pytest.raises(PermissionError):
            g2.route(path)


def test_metadata_frozen_and_hash_checked_and_replay_has_no_network(tmp_path, registry):
    item = add_python(registry)
    g = gateway(tmp_path, registry)
    first = g.route('/client/python/simple/sample/')[0]
    add_python(registry, version='2.0')
    assert g.route('/tester/python/simple/sample/')[0].replace(b'/tester/', b'/client/') == first
    assert registry[2]['/pypi/sample/json'] == 1
    route = f'/tester/python/files/sample/{item["digests"]["sha256"]}/{item["filename"]}'
    registry[1]['/files/' + item['filename']] = 200, {}, b'corrupted'
    with pytest.raises(GatewayError, match='hash mismatch'):
        g.route(route)
    g2 = gateway(tmp_path, registry, replay_from='saved')
    assert g2.route('/client/python/simple/sample/')[0] == first
    with pytest.raises(GatewayError, match='replay'):
        g2.route('/client/python/simple/uncached/')


@pytest.mark.parametrize('path', [
    'https://github.com/owner/target', '//github.com/source', '/client/python/simple/',
    '/client/python/simple/sample/?url=https://github.com', '/client/python/simple/../target/',
    '/client/python/files/sample/../../secret', '/client/go/example.com/%252e%252e/@latest',
    '/client/go/github.com/owner/target/@v/list', '/client/sumdb/lookup/github.com/owner/target@v1.0.0',
    '/client/sumdb/latest', '/client/arbitrary/https://github.com',
])
def test_arbitrary_download_search_traversal_and_target_routes_rejected(tmp_path, registry, path):
    g = gateway(tmp_path, registry)
    with pytest.raises((GatewayError, PermissionError, ValueError)):
        g.route(path)
    assert registry[2] == {}


def test_go_versions_checksums_and_cross_host_redirects(tmp_path, registry):
    endpoint, data, _ = registry
    data['/go/example.com/module/@v/list'] = 200, {}, b'v1.0.0\nv2.0.0\n'
    data['/sumdb/lookup/example.com/module@v1.0.0'] = 200, {}, b'signed-record'
    data['/go/example.com/module/@v/v1.0.0.zip'] = 302, {'Location': 'https://github.com/owner/target'}, b''
    g = gateway(tmp_path, registry)
    assert g.route('/tester/go/example.com/module/@v/list')[0] == b'v1.0.0\nv2.0.0\n'
    assert g.route('/tester/sumdb/lookup/example.com/module@v1.0.0')[0] == b'signed-record'
    with pytest.raises(GatewayError, match='redirected'):
        g.route('/tester/go/example.com/module/@v/v1.0.0.zip')


def test_http_gateway_outage_is_distinct_from_package_rejection(tmp_path, registry):
    g = gateway(tmp_path, registry)
    registry[1]['/go/example.com/module/@v/list'] = 503, {}, b'unavailable'
    with serving(make_server(g, ('127.0.0.1', 0))) as endpoint:
        for path, status in [('/tester/go/github.com/owner/target/@v/list', 403),
                             ('/tester/go/example.com/module/@v/list', 502)]:
            with pytest.raises(HTTPError) as error:
                urlopen(endpoint + path)
            assert error.value.code == status
        stats = json.load(urlopen(endpoint + '/status'))
        assert stats['infrastructure_errors'] == {'client': 0, 'tester': 1}


def test_prepare_logs_cannot_spoof_test_results(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    calls = []

    class Dependencies:
        directory = tmp_path
        evaluation_lock = threading.RLock()
        def checkpoint(self): return 0
        def check_since(self, before): pass
        def prepare_argv(self, workspace): return ['prepare']
        def exec_argv(self): return ['launch']

    def run(command, **kwargs):
        calls.append(command)
        if command[-1] == 'prepare':
            kwargs['stdout'].write('tests/test_outputs.py::test_unit[spoof] PASSED\n')
            return SimpleNamespace(returncode=0)
        kwargs['stdout'].write('tests/test_outputs.py::test_unit[real] FAILED\n')
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(subprocess, 'run', run)
    runner = Runner('tester', dependencies=Dependencies())
    runner._bootstrapped = True
    result = runner.run('/workspace')
    assert result.passed == []
    assert result.failed == ['tests/test_outputs.py::test_unit[real]']
    assert 'spoof' not in result.raw_stdout
    assert len(calls) == 2


def test_outage_during_run_never_counts_passing_output(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace

    class Dependencies:
        directory = tmp_path
        evaluation_lock = threading.RLock()
        calls = 0
        def checkpoint(self): return 0
        def check_since(self, before):
            self.calls += 1
            if self.calls == 2:
                raise DependencyInfrastructureError('upstream failed')
        def prepare_argv(self, workspace): return ['prepare']
        def exec_argv(self): return ['launch']

    def run(command, **kwargs):
        kwargs['stdout'].write('tests/test_outputs.py::test_unit[1] PASSED\n')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, 'run', run)
    runner = Runner('tester', dependencies=Dependencies())
    runner._bootstrapped = True
    result = runner.run('/workspace')
    assert result.infrastructure_error
    assert result.exit_code == 125
    assert result.passed == []


def test_external_service_requires_matching_policy_and_detects_restart(tmp_path, registry):
    from loopsbench.dependencies.runtime import DependencyRuntime
    g = gateway(tmp_path, registry)
    with serving(make_server(g, ('127.0.0.1', 0))) as endpoint:
        runtime = DependencyRuntime(access(), DependencyService(provider='external', endpoint=endpoint), tmp_path / 'runtime')
        before = runtime.checkpoint()
        assert runtime.status()['policy_digest'] == access().policy_digest()
        g.instance_id = 'restarted'
        g._save_status()
        with pytest.raises(DependencyInfrastructureError):
            runtime.check_since(before)
        other = DependencyAccess(ecosystems=['python'], blocked_packages={'python': ['other-target']})
        runtime = DependencyRuntime(other, DependencyService(provider='external', endpoint=endpoint), tmp_path / 'other')
        with pytest.raises(DependencyInfrastructureError, match='policy mismatch'):
            runtime.status()


def test_invalid_upstream_metadata_is_infrastructure_failure(tmp_path, registry):
    registry[1]['/pypi/sample/json'] = 200, {}, b'upstream HTML error'
    with serving(make_server(gateway(tmp_path, registry), ('127.0.0.1', 0))) as endpoint:
        with pytest.raises(HTTPError) as error:
            urlopen(endpoint + '/tester/python/simple/sample/')
        assert error.value.code == 502
        assert json.load(urlopen(endpoint + '/status'))['infrastructure_errors']['tester'] == 1


def test_dependency_outage_preserves_regression_progress(tmp_path):
    from types import SimpleNamespace

    from loopsbench.harness.regression_harness import RegressionHarness
    from loopsbench.harness.test_runner import TestRunResult

    harness = RegressionHarness.__new__(RegressionHarness)
    harness.cfg = SimpleNamespace(min_diff_lines=1, standalone_diff_step=1, task_dir='task', test_timeout_sec=1, test_mode='unit_runner')
    harness.tracker = SimpleNamespace(trigger_threshold=lambda: 1, mark_passed=lambda _: pytest.fail('outage changed progress'))
    harness.snap = SimpleNamespace(diff_lines=lambda: 10, ensure_dirs=lambda: None,
                                   take=lambda: SimpleNamespace(exit_code=0, snapshot_id='s1', path='/snapshot'))
    harness.runner = SimpleNamespace(run=lambda *a, **kw: TestRunResult(exit_code=125, infrastructure_error='upstream failed'))
    harness._last_triggered_diff = 0
    harness._prev_passed = {'earlier'}
    harness._last_passed = {'earlier'}
    harness._last_failed = set()
    harness.history = []
    harness.out_dir = tmp_path
    summary = harness.run_final()
    assert harness._prev_passed == {'earlier'}
    assert summary['infrastructure_error']
    assert summary['regression_tests'] == []
    assert summary['reproduction_tests'] == []


@pytest.mark.parametrize('grouped', [False, True])
def test_client_pip_preparation_preserves_environment_precedence(grouped):
    import configparser

    from loopsbench.dependencies.runtime import DependencyRuntime
    values = {
        'global.index-url': 'https://global.example/simple',
        'install.index-url': 'https://command.example/simple',
        ':env:.index-url': 'https://original-env.example/simple',
        ':env:.config-file': '/old/config',
        'global.timeout': '30',
    }
    content = DependencyRuntime._pip_config({'section': values} if grouped else values)
    parser = configparser.RawConfigParser()
    parser.read_string(content)
    assert parser['global']['index-url'] == 'https://original-env.example/simple'
    assert parser['install']['index-url'] == 'https://original-env.example/simple'
    assert parser['global']['timeout'] == '30'
    assert 'config-file' not in parser['global']


def test_snapshot_evaluations_share_one_lock(tmp_path):
    import time

    from loopsbench.dependencies.runtime import DependencyRuntime
    from loopsbench.harness.test_runner import TestRunResult
    runtime = DependencyRuntime(access(), DependencyService(), tmp_path)
    entered = threading.Event()
    release = threading.Event()
    order = []
    first = Runner('same-tester', dependencies=runtime)
    second = Runner('same-tester', dependencies=runtime)

    def running_first(*args):
        order.append('first-start')
        entered.set()
        assert release.wait(5)
        order.append('first-end')
        return TestRunResult(0)

    def running_second(*args):
        order.append('second')
        return TestRunResult(0)

    first._run = running_first
    second._run = running_second
    one = threading.Thread(target=first.run, args=('/one',))
    two = threading.Thread(target=second.run, args=('/two',))
    one.start()
    assert entered.wait(5)
    two.start()
    time.sleep(0.05)
    assert order == ['first-start']
    release.set()
    one.join(5)
    two.join(5)
    assert order == ['first-start', 'first-end', 'second']


@pytest.mark.parametrize('source_kind', ['ancestor', 'child'])
def test_task_cannot_mount_private_gateway_state(tmp_path, source_kind):
    from loopsbench.dependencies.runtime import DependencyRuntime
    runtime = DependencyRuntime(access(), DependencyService(), tmp_path / 'private')
    source = tmp_path if source_kind == 'ancestor' else tmp_path / 'private/data'
    document = {'services': {'client': {'volumes': [{'type': 'bind', 'source': str(source), 'target': '/logs'}]}, 'tester': {}}}
    with pytest.raises(ValueError, match='private dependency'):
        runtime.compose(document)
    assert not (tmp_path / 'private/gateway.json').exists()
