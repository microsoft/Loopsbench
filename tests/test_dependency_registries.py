from __future__ import annotations

import base64
import gzip
import hashlib
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from test_dependencies import registry as _registry
from test_dependencies import serving, wheel

from loopsbench.dependencies.config import DependencyAccess, DependencyService
from loopsbench.dependencies.prepare import (
    preparation_plugins,
    runtime_environment,
    tool_argv,
    write_configs,
)
from loopsbench.dependencies.protocol import check_package
from loopsbench.dependencies.server import Gateway, GatewayError, make_server

registry = _registry


def policy(**kwargs):
    return DependencyAccess(blocked_packages={
        "python": ["target-project"], "go": ["example.com/target"], "cargo": ["target_project"],
        "npm": ["@owner/target"], "maven": ["org.example:target"], "nuget": ["Target.Project"],
        "apt": ["target-project"],
    }, **kwargs)


def gateway(tmp_path, registry, access=None, **extra):
    endpoint = registry[0]
    service = DependencyService(
        npm_registry_url=endpoint + "/npm", cargo_index_url=endpoint + "/cargo",
        cargo_download_url=endpoint + "/crates/{crate}/{version}",
        maven_repository_urls=[endpoint + "/maven", endpoint + "/plugins"],
        nuget_flat_url=endpoint + "/nuget", python_simple_url=endpoint + "/simple",
        apt_repositories={"debian": {"url": endpoint + "/debian", "suites": ["bookworm"]}},
        allow_insecure_upstreams=True, **extra,
    )
    return Gateway({"policy": (access or policy()).wire_policy(), "service": service.model_dump()}, tmp_path / "data")


@pytest.mark.parametrize("kind,name", [("cargo", "target-project"), ("cargo", "TARGET_PROJECT"),
                                        ("npm", "@OWNER/Target"), ("nuget", "target.project"),
                                        ("maven", "org.example:target"), ("apt", "target-project")])
def test_all_protocols_check_canonical_target_names(kind, name):
    with pytest.raises(PermissionError):
        check_package(policy().wire_policy(), kind, name)


def test_access_and_install_plugins_are_independent(tmp_path):
    access = DependencyAccess(blocked_packages={"cargo": ["target"]})
    config = {"access": access.model_dump()}
    assert set(access.ecosystems) == {"python", "go", "npm", "cargo", "maven", "nuget", "apt"}
    assert preparation_plugins(config, tmp_path) == []
    (tmp_path / "Cargo.toml").touch()
    (tmp_path / "package.json").touch()
    assert set(preparation_plugins(config, tmp_path)) == {"cargo", "npm"}
    config["access"]["prepare"] = []
    assert preparation_plugins(config, tmp_path) == []
    with pytest.raises(ValueError, match="requires its package protocol"):
        DependencyAccess(ecosystems=["npm"], blocked_packages={"npm": ["target"]}, prepare=["cargo"])
    assert DependencyAccess(mode="disabled")


def add_npm(registry, package="@sample/lib", version="1.0.0", blob=b"tarball"):
    from urllib.parse import quote
    endpoint, data, _ = registry
    artifact = "/artifacts/lib-" + version + ".tgz"
    metadata = {"name": package, "dist-tags": {"latest": version}, "versions": {version: {
        "name": package, "version": version, "dist": {"tarball": endpoint + artifact,
            "integrity": "sha512-" + base64.b64encode(hashlib.sha512(blob).digest()).decode()}}}}
    data["/npm/" + quote(package, safe="@")] = 200, {}, json.dumps(metadata).encode()
    data[artifact] = 200, {}, blob
    return metadata


def test_npm_scoped_metadata_download_cache_policy_and_integrity(tmp_path, registry):
    add_npm(registry)
    g = gateway(tmp_path, registry)
    result = json.loads(g.route("/client/npm/@sample%2flib", "http://gateway")[0])
    assert result["versions"]["1.0.0"]["dist"]["tarball"] == "http://gateway/client/npm/_files/@sample/lib/1.0.0.tgz"
    route = "/tester/npm/_files/@sample/lib/1.0.0.tgz"
    assert g.route(route)[0] == b"tarball"
    assert g.route("/tester/npm/@sample/lib/-/lib-1.0.0.tgz")[0] == b"tarball"
    blocked = DependencyAccess(blocked_packages={"npm": ["@sample/lib"]})
    with pytest.raises(PermissionError):
        gateway(tmp_path, registry, blocked).route(route)
    # Hash checking also occurs on cache hits.
    artifact = next(v for k, v in g.index.items() if k.endswith(".tgz"))
    (tmp_path / "data/blobs" / artifact["sha256"]).write_bytes(b"corrupt")
    with pytest.raises(GatewayError, match="hash mismatch"):
        g.route(route)


def test_npm_rejects_cross_host_metadata_urls_and_search(tmp_path, registry):
    metadata = add_npm(registry, "sample")
    metadata["versions"]["1.0.0"]["dist"]["tarball"] = "https://github.com/owner/target/archive/main.tar.gz"
    registry[1]["/npm/sample"] = 200, {}, json.dumps(metadata).encode()
    g = gateway(tmp_path, registry)
    with pytest.raises(GatewayError, match="outside"):
        g.route("/tester/npm/_files/sample/1.0.0.tgz")
    for path in ("/-/v1/search", "/-/all", "/-/npm/v1/security/audits"):
        with pytest.raises((ValueError, GatewayError)):
            g.route("/client/npm" + path)


def test_cargo_sparse_index_crate_checksums_and_alias_block(tmp_path, registry):
    blob = b"crate bytes"
    row = {"name": "sample", "vers": "1.2.3", "deps": [], "features": {}, "yanked": False,
           "cksum": hashlib.sha256(blob).hexdigest()}
    registry[1]["/cargo/sa/mp/sample"] = 200, {}, json.dumps(row).encode() + b"\n"
    registry[1]["/crates/sample/1.2.3"] = 200, {}, blob
    g = gateway(tmp_path, registry)
    assert json.loads(g.route("/tester/cargo/index/config.json", "http://gateway")[0])["dl"].startswith("http://gateway/tester/")
    assert g.route("/tester/cargo/files/sample/1.2.3/download")[0] == blob
    for path in ("/client/cargo/index/ta/rg/target-project", "/tester/cargo/files/target_project/1.0.0/download"):
        with pytest.raises(PermissionError):
            g.route(path)
    assert not any("target" in url for url in registry[2])
    with pytest.raises(GatewayError):
        g.route("/tester/cargo/index/wrong/sample")


@pytest.mark.parametrize("suffix", ["1.0/target-1.0.pom", "1.0/target-1.0-sources.jar", "maven-metadata.xml",
                                    "1.0-SNAPSHOT/maven-metadata.xml.sha256", "1.0/target-1.0.jar.sha1"])
def test_maven_blocks_target_for_all_classifiers_and_metadata(tmp_path, registry, suffix):
    with pytest.raises(PermissionError):
        gateway(tmp_path, registry).route("/tester/maven/org/example/target/" + suffix)
    assert registry[2] == {}


def test_maven_mirrors_fallback_only_on_missing_artifacts(tmp_path, registry):
    path = "/org/example/lib/1.0/lib-1.0.jar"
    registry[1]["/plugins" + path] = 200, {}, b"plugin"
    g = gateway(tmp_path, registry)
    assert g.route("/tester/maven" + path)[0] == b"plugin"
    registry[1]["/maven" + path + ".sha1"] = 503, {}, b"unavailable"
    with pytest.raises(GatewayError) as error:
        g.route("/tester/maven" + path + ".sha1")
    assert error.value.status == 502
    assert "/plugins" + path + ".sha1" not in registry[2]


def test_nuget_v3_flat_container_and_identity_checks(tmp_path, registry):
    registry[1]["/nuget/sample/1.0.0/sample.1.0.0.nupkg"] = 200, {}, b"nupkg"
    g = gateway(tmp_path, registry)
    resources = json.loads(g.route("/client/nuget/index.json", "http://gateway")[0])["resources"]
    assert resources[0]["@id"] == "http://gateway/client/nuget/flat/"
    assert g.route("/tester/nuget/flat/sample/1.0.0/sample.1.0.0.nupkg")[0] == b"nupkg"
    with pytest.raises(PermissionError):
        g.route("/tester/nuget/flat/target.project/index.json")
    with pytest.raises(GatewayError, match="identity"):
        g.route("/tester/nuget/flat/sample/1.0.0/target.project.1.0.0.nupkg")


def test_apt_preserves_signed_indexes_and_uses_index_identity_for_artifacts(tmp_path, registry):
    allowed = "pool/main/l/lib/lib_1.0_amd64.deb"
    blocked = "pool/main/i/innocent/innocent_1.0_amd64.deb"
    body = b"deb bytes"
    digest = hashlib.sha256(body).hexdigest()
    index = (f"Package: lib\nFilename: {allowed}\nSHA256: {digest}\n\n"
             f"Package: innocent\nSource: target-project (1.0)\nFilename: {blocked}\nSHA256: {digest}\n\n").encode()
    registry[1]["/debian/dists/bookworm/InRelease"] = 200, {}, b"signed release unchanged"
    registry[1]["/debian/dists/bookworm/main/binary-amd64/Packages.gz"] = 200, {}, gzip.compress(index)
    registry[1]["/debian/" + allowed] = 200, {}, body
    g = gateway(tmp_path, registry)
    assert g.route("/tester/apt/debian/dists/bookworm/InRelease")[0] == b"signed release unchanged"
    # Artifact lookup works even before the client's first index request.
    assert g.route("/tester/apt/debian/" + allowed)[0] == body
    with pytest.raises(PermissionError):
        g.route("/tester/apt/debian/" + blocked)
    assert "/debian/" + blocked not in registry[2]
    with pytest.raises(GatewayError):
        g.route("/tester/apt/debian/dists/unknown/InRelease")


def test_standard_python_simple_mirror_relative_links(tmp_path, registry):
    body = wheel("sample", "1.0")
    digest = hashlib.sha256(body).hexdigest()
    registry[1]["/simple/sample/"] = 200, {}, (
        '<a href="../../files/sample-1.0-py3-none-any.whl#sha256=' + digest + '" data-requires-python="&gt;=3.8">sample</a>').encode()
    registry[1]["/files/sample-1.0-py3-none-any.whl"] = 200, {}, body
    g = gateway(tmp_path, registry)
    page = g.route("/client/python/simple/sample/")[0]
    assert b'data-requires-python="&gt;=3.8"' in page
    assert g.route("/tester/python/files/sample/" + digest + "/sample-1.0-py3-none-any.whl")[0] == body


def test_only_trusted_pinned_source_ids_can_download(tmp_path, registry):
    body = b"fixed toolchain archive"
    g = gateway(tmp_path, registry)
    g.policy["sources"] = {"compiler": {"url": registry[0] + "/compiler.tar.gz", "filename": "compiler.tar.gz",
                                        "sha256": hashlib.sha256(body).hexdigest(), "redirect_hosts": []}}
    registry[1]["/compiler.tar.gz"] = 200, {}, body
    assert g.route("/tester/source/compiler/compiler.tar.gz")[0] == body
    with pytest.raises(PermissionError):
        g.route("/client/source/unknown/compiler.tar.gz")
    with pytest.raises(GatewayError):
        g.route("/client/source/compiler/compiler.tar.gz?url=https://github.com/owner/target")


def test_head_is_audited_and_disabled_protocols_stay_blocked(tmp_path, registry):
    g = gateway(tmp_path, registry, DependencyAccess(ecosystems=["python"], blocked_packages={"python": ["target"]}))
    with serving(make_server(g, ("127.0.0.1", 0))) as endpoint:
        for suffix in ("cargo/index/config.json", "nuget/index.json", "maven/org/example/lib/maven-metadata.xml", "apt/debian/dists/bookworm/InRelease"):
            with pytest.raises(HTTPError) as error:
                urlopen(Request(endpoint + "/tester/" + suffix, method="HEAD"))
            assert error.value.code == 403
        assert json.load(urlopen(endpoint + "/status"))["infrastructure_errors"]["tester"] == 0


def test_generated_tool_configuration_keeps_signatures_and_overrides_offline_defaults(tmp_path, monkeypatch):
    import loopsbench.dependencies.prepare as preparation
    monkeypatch.setattr(preparation, "STATE", tmp_path)
    config = {"access": policy().model_dump(), "endpoint": "http://gateway:8080",
              "apt_repositories": {"debian": {"suites": ["bookworm"], "components": ["main"],
                                              "architectures": ["amd64"], "signed_by": "/image/keyring.gpg"}}}
    write_configs(config, "tester")
    env = runtime_environment(config, "tester", {"CARGO_NET_OFFLINE": "true", "RUSTUP_HOME": "/installed/rustup"})
    assert env["CARGO_NET_OFFLINE"] == "false"
    assert env["RUSTUP_HOME"] == "/installed/rustup"
    assert "replace-with" in (tmp_path / "cargo/config.toml").read_text()
    assert "signed-by=/image/keyring.gpg" in (tmp_path / "apt.sources.list").read_text()
    assert "trusted=yes" not in (tmp_path / "apt.sources.list").read_text()
    assert "-Dmaven.repo.local=" + str(tmp_path / "maven") in tool_argv("mvn", ["test"], "tester")
    assert tool_argv("dotnet", ["--info"], "tester") == ["dotnet", "--info"]
