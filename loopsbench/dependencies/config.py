"""Trusted task policy. Never derive deny rules from a submitted manifest."""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from loopsbench.dependencies.protocol import ECOSYSTEMS, canonical_package
from loopsbench.terminal.network_policy import NetworkPlan, NetworkPolicy

Ecosystem = Literal["python", "go", "npm", "cargo", "maven", "nuget", "apt"]
Preparation = Literal["python", "go", "npm", "cargo", "maven", "gradle", "nuget"]


class TrustedSource(BaseModel):
    """An exact, operator-approved archive, toolchain or Git bundle."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    url: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    filename: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")
    redirect_hosts: list[str] = Field(default_factory=list)

    @field_validator("redirect_hosts")
    @classmethod
    def exact_redirect_hosts(cls, values):
        if any("*" in host or "/" in host for host in values):
            raise ValueError("source redirect hosts must be exact hostnames")
        return NetworkPolicy.validate_hosts(values)

    @model_validator(mode="after")
    def validate_source(self):
        validate_url(self.url)
        NetworkPolicy.validate_hosts(self.redirect_hosts)
        return self


class AptRepository(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    url: str
    suites: list[str]
    components: list[str] = Field(default_factory=lambda: ["main"])
    architectures: list[str] = Field(default_factory=lambda: ["amd64"])
    # Existing task-image keyring; gateway never disables apt signature checks.
    signed_by: str = "/usr/share/keyrings/debian-archive-keyring.gpg"

    @model_validator(mode="after")
    def validate_repository(self):
        import re
        for values in (self.suites, self.components, self.architectures):
            if not values or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", v) for v in values):
                raise ValueError("apt suites/components/architectures must be nonempty path segments")
        if not re.fullmatch(r"/[A-Za-z0-9/_.-]+", self.signed_by) or ".." in self.signed_by.split("/"):
            raise ValueError("signed_by must be an absolute keyring path")
        return self


def validate_url(url: str, insecure: bool = False):
    p = urlsplit(url)
    if (p.scheme not in ({"http", "https"} if insecure else {"https"}) or not p.hostname
            or p.username or p.password or p.query or p.fragment):
        raise ValueError("dependency URLs require HTTPS without credentials/query/fragment (HTTP needs allow_insecure_upstreams)")
    _ = p.port


class PythonPreparation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    interpreter: str = "python3"
    # These are trusted test/tool requirements; project requirements come from
    # the submitted project/requirements files on every snapshot.
    test_requirements: list[str] = Field(default_factory=list)
    requirements_files: list[str] = Field(default_factory=list)
    project: str | None = "."

    @field_validator("requirements_files")
    @classmethod
    def relative_files(cls, values):
        for value in values:
            validate_relative_path(value)
        return values

    @field_validator("project")
    @classmethod
    def relative_project(cls, value):
        if value is not None:
            validate_relative_path(value)
        return value


def validate_relative_path(value: str) -> str:
    if not value or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts or "\x00" in value:
        raise ValueError("dependency project paths must be relative to the snapshot")
    return value


class NpmPreparation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # Explicit opt-in for legacy upgrade tasks whose lockfile is only a seed.
    install_mode: Literal["locked", "update"] = "locked"
    legacy_peer_deps: bool = False


class DependencyAccess(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["managed", "disabled"] = "managed"
    ecosystems: list[Ecosystem] = Field(default_factory=lambda: list(ECOSYSTEMS))
    blocked_packages: dict[Ecosystem, list[str]] = Field(default_factory=dict)
    # A trusted identity list permits version upgrades without exposing every
    # registry package/fork. None retains the original blacklist-only mode.
    allowed_packages: dict[Ecosystem, list[str]] | None = None
    # None detects root manifests. Explicit [] exposes protocols without an
    # automatic install. Enabling a protocol never requires that tool installed.
    prepare: list[Preparation] | None = None
    projects: dict[Preparation, list[str]] = Field(default_factory=dict)
    sources: dict[str, TrustedSource] = Field(default_factory=dict)
    python: PythonPreparation = Field(default_factory=PythonPreparation)
    npm: NpmPreparation = Field(default_factory=NpmPreparation)
    go_modules: list[str] = Field(default_factory=lambda: ["."])
    prepare_timeout_sec: int = Field(default=600, ge=1, le=3600)

    @field_validator("go_modules")
    @classmethod
    def relative_modules(cls, values):
        return [validate_relative_path(value) for value in values]

    @model_validator(mode="after")
    def validate_policy(self):
        import re
        if self.mode == "managed" and not self.ecosystems:
            raise ValueError("managed dependency access requires explicit ecosystems")
        if len(set(self.ecosystems)) != len(self.ecosystems):
            raise ValueError("duplicate dependency ecosystems")
        if self.mode == "managed" and not any(self.blocked_packages.values()):
            raise ValueError("declare trusted target identities and aliases in blocked_packages")
        for ecosystem, packages in self.blocked_packages.items():
            for package in packages:
                canonical_package(ecosystem, package)
        if self.allowed_packages is not None:
            if set(self.allowed_packages) != set(self.ecosystems):
                raise ValueError("allowed_packages must declare every enabled ecosystem, including empty lists")
            for ecosystem, packages in self.allowed_packages.items():
                for package in packages:
                    canonical_package(ecosystem, package)
        for kind in [*(self.prepare or []), *self.projects]:
            if ("maven" if kind == "gradle" else kind) not in self.ecosystems:
                raise ValueError(f"preparation plugin {kind} requires its package protocol enabled")
        if self.prepare is not None and len(set(self.prepare)) != len(self.prepare):
            raise ValueError("duplicate preparation plugins")
        if "python" in self.projects:
            raise ValueError("configure Python project paths with python.project and python.requirements_files")
        for paths in self.projects.values():
            if not paths:
                raise ValueError("project lists must not be empty")
            for path in paths:
                validate_relative_path(path)
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name) for name in self.sources):
            raise ValueError("source IDs must be lowercase path segments")
        return self

    def wire_policy(self) -> dict:
        policy = {"ecosystems": sorted(self.ecosystems), "blocked_packages": {
            kind: sorted({canonical_package(kind, value) for value in values})
            for kind, values in sorted(self.blocked_packages.items())
        }}
        if self.sources:
            policy["sources"] = {name: source.model_dump() for name, source in sorted(self.sources.items())}
        if self.allowed_packages is not None:
            policy["allowed_packages"] = {
                kind: sorted({canonical_package(kind, name) for name in names})
                for kind, names in sorted(self.allowed_packages.items())
            }
        return policy

    def policy_digest(self) -> str:
        return hashlib.sha256(json.dumps(self.wire_policy(), sort_keys=True).encode()).hexdigest()


class DependencyService(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["docker", "external"] = "docker"
    # An external service implements this gateway protocol and serves ONLY this
    # policy. A generic PyPI/Go proxy is an upstream, never a policy gateway.
    endpoint: str | None = None
    python_metadata_url: str = "https://pypi.org/pypi"
    # PEP 503 Simple API supports standard artifact-server mirrors.
    python_simple_url: str | None = None
    python_artifact_hosts: list[str] = Field(default_factory=lambda: ["files.pythonhosted.org"])
    go_proxy_url: str = "https://proxy.golang.org"
    go_artifact_hosts: list[str] = Field(default_factory=lambda: ["storage.googleapis.com"])
    go_sumdb_url: str = "https://sum.golang.org"
    npm_registry_url: str = "https://registry.npmjs.org"
    npm_artifact_hosts: list[str] = Field(default_factory=list)
    cargo_index_url: str = "https://index.crates.io"
    cargo_download_url: str = "https://static.crates.io/crates/{crate}/{crate}-{version}.crate"
    cargo_artifact_hosts: list[str] = Field(default_factory=list)
    maven_repository_urls: list[str] = Field(default_factory=lambda: ["https://repo.maven.apache.org/maven2", "https://plugins.gradle.org/m2"])
    maven_artifact_hosts: list[str] = Field(default_factory=lambda: ["plugins-artifacts.gradle.org", "repo.maven.apache.org"])
    nuget_flat_url: str = "https://api.nuget.org/v3-flatcontainer"
    nuget_artifact_hosts: list[str] = Field(default_factory=list)
    apt_repositories: dict[str, AptRepository] = Field(default_factory=dict)
    allow_insecure_upstreams: bool = False
    request_timeout_sec: int = Field(default=45, ge=1, le=300)
    max_concurrent_downloads: int = Field(default=4, ge=1, le=32)
    max_concurrent_requests: int = Field(default=4, ge=1, le=16)
    max_concurrent_npm_requests: int = Field(default=2, ge=1, le=8)
    max_pending_requests: int = Field(default=32, ge=1, le=64)
    max_npm_metadata_bytes: int = Field(default=16 * 1024 * 1024, ge=1024)
    max_artifact_bytes: int = Field(default=256 * 1024 * 1024, ge=1024)
    max_cache_bytes: int = Field(default=4 * 1024**3, ge=1024)
    # Optional prior gateway data directory. Copy it; never mount it writable in
    # the new trial. Replay mode fails on cache misses instead of going online.
    replay_from: str | None = None

    @model_validator(mode="after")
    def validate_service(self):
        if (self.provider == "external") != bool(self.endpoint):
            raise ValueError("external dependency service requires endpoint; docker service must not set it")
        if self.provider == "external" and self.replay_from:
            raise ValueError("configure replay at the external service")
        import re
        if not self.maven_repository_urls:
            raise ValueError("maven_repository_urls must not be empty")
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name) for name in self.apt_repositories):
            raise ValueError("apt repository IDs must be lowercase path segments")
        if set(re.findall(r"\{([^}]+)\}", self.cargo_download_url)) - {"crate", "version", "prefix", "lowerprefix"}:
            raise ValueError("unknown Cargo download URL placeholder")
        for url in [self.python_metadata_url, self.python_simple_url, self.go_proxy_url, self.go_sumdb_url,
                    self.npm_registry_url, self.cargo_index_url, self.cargo_download_url,
                    *self.maven_repository_urls, self.nuget_flat_url,
                    *(repo.url for repo in self.apt_repositories.values()), self.endpoint]:
            if url is None:
                continue
            validate_url(url, self.allow_insecure_upstreams or url == self.endpoint)
            p = urlsplit(url)
            if url == self.endpoint and p.path not in {"", "/"}:
                raise ValueError("external gateway endpoint must be a dedicated origin without a path prefix")
            if url != self.endpoint and p.scheme != "https" and not self.allow_insecure_upstreams:
                raise ValueError("HTTP upstreams require allow_insecure_upstreams (local integration tests only)")
        for host in [*self.python_artifact_hosts, *self.go_artifact_hosts, *self.npm_artifact_hosts,
                     *self.cargo_artifact_hosts, *self.maven_artifact_hosts, *self.nuget_artifact_hosts]:
            if "*" in host or "/" in host:
                raise ValueError("artifact hosts must be exact hostnames")
            NetworkPolicy.validate_hosts([host])
        return self


def resolve_dependency_service(
    task: DependencyService | None, run: DependencyService | None,
) -> DependencyService:
    """Use task defaults, then apply only explicitly supplied run settings.

    Resolve each trial independently so task repositories never leak into other
    tasks. Lists and mappings are replaced as whole fields, not combined.
    """
    values = task.model_dump(exclude_unset=True) if task is not None else {}
    if run is not None:
        values.update(run.model_dump(exclude_unset=True))
    return DependencyService.model_validate(values)


def dependency_network(plan: NetworkPlan, access: DependencyAccess, endpoint: str) -> NetworkPlan:
    """Only implicit offline defaults may be replaced by managed access."""
    if access.mode != "managed":
        return plan
    for phase in ("agent", "verifier"):
        policy = getattr(plan, phase)
        if phase in plan.model_fields_set and policy.network_mode == "no-network":
            raise ValueError(f"dependency_access=managed conflicts with explicit network.{phase}=no-network")
        if policy.network_mode == "public" or (phase == "verifier" and policy.allowed_hosts):
            raise ValueError(f"managed dependencies require a restricted {phase} policy")
    host = urlsplit(endpoint).hostname
    if not host:
        raise ValueError("dependency gateway endpoint has no hostname")
    return plan.model_copy(update={
        "agent": NetworkPolicy(network_mode="allowlist", allowed_hosts=[*plan.agent.allowed_hosts, host]),
        "verifier": NetworkPolicy(network_mode="allowlist", allowed_hosts=[host]),
    })
