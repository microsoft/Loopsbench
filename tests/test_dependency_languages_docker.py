"""Opt-in end-to-end language plugins, using real package managers/registries."""
from __future__ import annotations

import json
import os
import uuid
from urllib.request import urlopen

import pytest

from loopsbench.dependencies.config import DependencyAccess, DependencyService
from loopsbench.harness.test_runner import TestRunner as Runner
from loopsbench.terminal.docker_compose_manager import DockerComposeManager
from loopsbench.terminal.network_policy import NetworkPlan
from loopsbench.terminal.terminal import Terminal

pytestmark = pytest.mark.skipif(os.environ.get("LOOPSBENCH_TEST_DEPENDENCIES_DOCKER") != "1", reason="opt-in Docker dependency integration")


@pytest.mark.parametrize("plugin", ["npm", "cargo", "maven", "maven-reactor", "gradle", "nuget"])
def test_language_install_change_and_rollback_with_restricted_network(tmp_path, plugin):
    tool = "maven" if plugin == "maven-reactor" else plugin
    image = os.environ.get("LOOPSBENCH_TEST_DEPENDENCIES_" + tool.upper() + "_IMAGE", "loopsbench-dep-" + tool + "-test:local")
    versions = {"npm": ["1.1.3", "1.3.0"], "cargo": ["1.0.14", "1.0.15"], "maven": ["3.14.0", "3.17.0"],
                "gradle": ["3.14.0", "3.17.0"], "nuget": ["13.0.1", "13.0.3"]}[tool]
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    for version in versions:
        project = snapshots / version
        project.mkdir()
        (project / "expected").write_text(version)
        if plugin == "npm":
            (project / "package.json").write_text(json.dumps({"name": "candidate", "version": "1.0.0", "dependencies": {"left-pad": version}}))
            # A pre-existing public-registry lockfile must still work without
            # granting that registry's hostname to the task containers.
            with urlopen("https://registry.npmjs.org/left-pad/" + version, timeout=30) as response:
                dist = json.load(response)["dist"]
            (project / "package-lock.json").write_text(json.dumps({"name": "candidate", "version": "1.0.0", "lockfileVersion": 3,
                "packages": {"": {"name": "candidate", "version": "1.0.0", "dependencies": {"left-pad": version}},
                             "node_modules/left-pad": {"version": version, "resolved": dist["tarball"], "integrity": dist["integrity"]}}}))
        elif plugin == "cargo":
            (project / "src").mkdir()
            (project / "Cargo.toml").write_text('[package]\nname="candidate"\nversion="0.1.0"\nedition="2021"\n'
                                               '[dependencies]\nitoa="=' + version + '"\n')
            (project / "src/main.rs").write_text('fn main() { assert_eq!(itoa::Buffer::new().format(123), "123"); }\n')
        elif plugin == "nuget":
            (project / "Candidate.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
                '<TargetFramework>net8.0</TargetFramework></PropertyGroup><ItemGroup><PackageReference Include="Newtonsoft.Json" Version="' + version + '" /></ItemGroup></Project>')
            (project / "Program.cs").write_text('if(Newtonsoft.Json.JsonConvert.SerializeObject(123) != "123") throw new System.Exception();')
        else:
            (project / "src/main/java").mkdir(parents=True)
            (project / "src/main/java/App.java").write_text('import org.apache.commons.lang3.StringUtils; class App { public static void main(String[] a) { if (!StringUtils.reverse("abc").equals("cba")) throw new Error(); }}')
            if plugin == "gradle":
                (project / "settings.gradle").write_text("rootProject.name='candidate'\n")
                (project / "build.gradle").write_text("plugins { id 'java' }\nrepositories { mavenCentral() }\n"
                    "dependencies { implementation 'org.apache.commons:commons-lang3:" + version + "' }\n")
            else:
                (project / "pom.xml").write_text('<project><modelVersion>4.0.0</modelVersion><groupId>bench</groupId><artifactId>candidate</artifactId>'
                '<version>1.0</version><properties><maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target></properties>'
                '<dependencies><dependency><groupId>org.apache.commons</groupId><artifactId>commons-lang3</artifactId><version>' + version + '</version></dependency></dependencies></project>')
            if plugin == "maven-reactor":
                (project / "helper/src/main/java").mkdir(parents=True)
                (project / "app").mkdir()
                (project / "src").rename(project / "app/src")
                (project / "helper/src/main/java/Helper.java").write_text('public class Helper { public static String text() { return "abc"; }}')
                app = project / "app/src/main/java/App.java"
                app.write_text(app.read_text().replace('reverse("abc")', 'reverse(Helper.text())'))
                parent = '<parent><groupId>bench</groupId><artifactId>candidate</artifactId><version>1.0</version></parent>'
                (project / "helper/pom.xml").write_text('<project><modelVersion>4.0.0</modelVersion>' + parent + '<artifactId>helper</artifactId></project>')
                (project / "app/pom.xml").write_text('<project><modelVersion>4.0.0</modelVersion>' + parent + '<artifactId>app</artifactId>'
                    '<dependencies><dependency><groupId>bench</groupId><artifactId>helper</artifactId><version>1.0</version></dependency></dependencies></project>')
                pom = project / "pom.xml"
                pom.write_text(pom.read_text().replace('</project>', '<packaging>pom</packaging><modules><module>helper</module><module>app</module></modules></project>'))
    scripts = {
        "npm": "node -e \"const fs=require('fs'); if(require('left-pad/package.json').version!==fs.readFileSync('expected','utf8')) process.exit(1); if(require('left-pad')('a',3)!=='  a') process.exit(2)\"\n",
        "cargo": "cargo run --quiet\ncargo metadata --format-version=1 > /tmp/metadata.json\npython3 -c \"import json; assert next(p['version'] for p in json.load(open('/tmp/metadata.json'))['packages'] if p['name']=='itoa')==open('expected').read()\"\n",
        "maven": 'mvn -B -q test\njava -cp "target/classes:/tmp/loopsbench-dependencies/maven/org/apache/commons/commons-lang3/$(cat expected)/commons-lang3-$(cat expected).jar" App\n',
        "maven-reactor": 'mvn -B -q test\njava -cp "app/target/classes:helper/target/classes:/tmp/loopsbench-dependencies/maven/org/apache/commons/commons-lang3/$(cat expected)/commons-lang3-$(cat expected).jar" App\n',
        "gradle": 'gradle --quiet classes\njar=$(find "/tmp/loopsbench-dependencies/gradle/caches/modules-2/files-2.1/org.apache.commons/commons-lang3/$(cat expected)" -name "*.jar" -print -quit)\ntest -n "$jar"\njava -cp "build/classes/java/main:$jar" App\n',
        "nuget": 'dotnet build -c Release\ndotnet bin/Release/net8.0/Candidate.dll\npython3 -c "import json; assert \'Newtonsoft.Json/\'+open(\'expected\').read() in json.load(open(\'obj/project.assets.json\'))[\'libraries\']"\n',
    }
    script = tmp_path / "run-tests.sh"
    script.write_text("set -e\n" + scripts[plugin] + "echo 'tests/test_dependency.py::version PASSED'\n")
    name = "lb-dep-" + plugin + "-" + uuid.uuid4().hex[:10]
    common = {"image": image, "entrypoint": ["sh", "-c"], "command": ["mkdir -p /workspace; exec sleep infinity"],
              "working_dir": "/workspace", "tmpfs": ["/workspace"]}
    compose = tmp_path / "compose.json"
    compose.write_text(json.dumps({"services": {
        "client": {**common, "container_name": name + "-client"},
        "tester": {**common, "container_name": name + "-tester"},
    }}))
    overlay = tmp_path / "harness.json"
    overlay.write_text(json.dumps({"services": {"tester": {"volumes": [f"{snapshots}:/snapshots:ro", f"{script}:/task-assets/run-tests.sh:ro"]}}}))
    ecosystem = "maven" if plugin in {"gradle", "maven-reactor"} else plugin
    target = {"npm": "candidate", "cargo": "candidate", "maven": "bench:candidate", "nuget": "Candidate"}[ecosystem]
    access = DependencyAccess(ecosystems=[ecosystem], blocked_packages={ecosystem: [target, "bench:helper", "bench:app"] if plugin == "maven-reactor" else [target]})
    manager = DockerComposeManager(client_container_name=name + "-client", client_image_name=image,
        tester_container_name=name + "-tester", tester_image_name=image, docker_compose_path=[compose, overlay],
        no_rebuild=True, logs_path=tmp_path / "logs", network=NetworkPlan(), dependency_access=access, dependency_service=DependencyService())
    try:
        client = manager.start()
        if plugin == "npm":
            baseline = client.exec_run(["npm", "config", "get", "registry"])
            assert baseline.exit_code == 0, baseline.output.decode()
            assert b"loopsbench-dependencies" not in baseline.output
        runner = Runner(name + "-tester", dependencies=manager.dependencies)
        with manager.agent_network_phase():
            if plugin == "npm":
                solving = client.exec_run(["npm", "config", "get", "registry"])
                assert b"loopsbench-dependencies" in solving.output, solving.output.decode()
            for version in [*versions, versions[0]]:
                result = runner.run("/snapshots/" + version, timeout_sec=600)
                assert not result.infrastructure_error, result
                assert result.exit_code == 0, result.raw_stdout + result.raw_stderr
                assert result.passed == ["tests/test_dependency.py::version"]
            for container in [client, manager.tester_container]:
                probe = "import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('https://github.com/owner/target',timeout=3)"
                assert container.exec_run(["python3", "-c", probe]).exit_code != 0
        if plugin == "npm":
            assert client.exec_run(["npm", "config", "get", "registry"]).output == baseline.output
        assert len(list(manager.dependencies.directory.glob("prepare-*.log"))) == 3
    finally:
        manager.stop()


def test_apt_signed_install_and_fresh_tester_for_regression_and_direct_snapshots(tmp_path):
    image = os.environ.get("LOOPSBENCH_TEST_DEPENDENCIES_APT_IMAGE", "loopsbench-dep-apt-test:local")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "sentinel").write_text("submitted content")
    script = tmp_path / "run-tests.sh"
    script.write_text("set -e\n"
        "test \"$(cat sentinel)\" = 'submitted content'\n"
        "test ! -e /usr/local/loopsbench-previous-evaluation\n"
        "if dpkg-query -s hello >/dev/null 2>&1; then exit 11; fi\n"
        "apt-get update -qq\napt-get install -y --no-install-recommends hello\n"
        "hello >/dev/null\ntouch /usr/local/loopsbench-previous-evaluation\n"
        "echo 'tests/test_dependency.py::version PASSED'\n")
    name = "lb-dep-apt-" + uuid.uuid4().hex[:10]
    common = {"image": image, "entrypoint": ["sh", "-c"], "command": ["mkdir -p /workspace; exec sleep infinity"],
              "working_dir": "/workspace", "tmpfs": ["/workspace"]}
    compose = tmp_path / "compose.json"
    compose.write_text(json.dumps({"services": {
        "client": {**common, "container_name": name + "-client"},
        "tester": {**common, "container_name": name + "-tester"},
    }}))
    overlay = tmp_path / "harness.json"
    overlay.write_text(json.dumps({"services": {"tester": {"volumes": [f"{snapshot}:/snapshot:ro", f"{script}:/task-assets/run-tests.sh:ro"]}}}))
    access = DependencyAccess(ecosystems=["apt"], blocked_packages={"apt": ["target-project"]})
    service = DependencyService(apt_repositories={"debian": {"url": "https://deb.debian.org/debian", "suites": ["bookworm"]}})
    manager = DockerComposeManager(client_container_name=name + "-client", client_image_name=image,
        tester_container_name=name + "-tester", tester_image_name=image, docker_compose_path=[compose, overlay],
        no_rebuild=True, logs_path=tmp_path / "logs", network=NetworkPlan(), dependency_access=access, dependency_service=service)
    try:
        client = manager.start()
        terminal = Terminal(manager, client, manager.tester_container)
        runner = Runner(name + "-tester", dependencies=manager.dependencies)
        ids = []
        for source in ["/snapshot", "/snapshot", "/workspace"]:
            if source == "/workspace":
                assert manager.tester_container.exec_run(["sh", "-c", "cp -a /snapshot/. /workspace/"]).exit_code == 0
            result = runner.run(source, timeout_sec=600)
            assert not result.infrastructure_error, result
            assert result.exit_code == 0, result.raw_stdout + result.raw_stderr
            assert result.passed == ["tests/test_dependency.py::version"]
            assert terminal.tester_container.id == manager.tester_container.id
            ids.append(manager.tester_container.id)
        assert len(set(ids)) == 3
    finally:
        manager.stop()
