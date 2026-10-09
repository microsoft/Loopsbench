"""Trusted task policy. Never derive deny rules from a submitted manifest."""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from loopsbench.dependencies.protocol import canonical_package
from loopsbench.terminal.network_policy import NetworkPlan, NetworkPolicy

Ecosystem = Literal["python", "go"]


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


class DependencyAccess(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["managed", "disabled"] = "managed"
    ecosystems: list[Ecosystem] = Field(default_factory=list)
    blocked_packages: dict[Ecosystem, list[str]] = Field(default_factory=dict)
    python: PythonPreparation = Field(default_factory=PythonPreparation)
    go_modules: list[str] = Field(default_factory=lambda: ["."])
    prepare_timeout_sec: int = Field(default=600, ge=1, le=3600)

    @field_validator("go_modules")
    @classmethod
    def relative_modules(cls, values):
        return [validate_relative_path(value) for value in values]

    @model_validator(mode="after")
    def validate_policy(self):
        if self.mode == "managed" and not self.ecosystems:
            raise ValueError("managed dependency access requires explicit ecosystems")
        if len(set(self.ecosystems)) != len(self.ecosystems):
            raise ValueError("duplicate dependency ecosystems")
        for ecosystem in self.ecosystems:
            if not self.blocked_packages.get(ecosystem):
                raise ValueError(f"declare the target package and aliases in blocked_packages.{ecosystem}")
        for ecosystem, packages in self.blocked_packages.items():
            for package in packages:
                canonical_package(ecosystem, package)
        if "go" in self.ecosystems and not self.go_modules:
            raise ValueError("go_modules must list at least one snapshot module directory")
        return self

    def wire_policy(self) -> dict:
        return {"ecosystems": sorted(self.ecosystems), "blocked_packages": {
            kind: sorted({canonical_package(kind, value) for value in values})
            for kind, values in sorted(self.blocked_packages.items())
        }}

    def policy_digest(self) -> str:
        return hashlib.sha256(json.dumps(self.wire_policy(), sort_keys=True).encode()).hexdigest()


class DependencyService(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["docker", "external"] = "docker"
    # An external service implements this gateway protocol and serves ONLY this
    # policy. A generic PyPI/Go proxy is an upstream, never a policy gateway.
    endpoint: str | None = None
    python_metadata_url: str = "https://pypi.org/pypi"
    python_artifact_hosts: list[str] = Field(default_factory=lambda: ["files.pythonhosted.org"])
    go_proxy_url: str = "https://proxy.golang.org"
    go_artifact_hosts: list[str] = Field(default_factory=lambda: ["storage.googleapis.com"])
    go_sumdb_url: str = "https://sum.golang.org"
    allow_insecure_upstreams: bool = False
    request_timeout_sec: int = Field(default=45, ge=1, le=300)
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
        for url in [self.python_metadata_url, self.go_proxy_url, self.go_sumdb_url, self.endpoint]:
            if url is None:
                continue
            p = urlsplit(url)
            if (p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password
                    or p.query or p.fragment):
                raise ValueError("dependency URLs must be HTTP(S) URLs without credentials/query/fragment")
            _ = p.port
            if url == self.endpoint and p.path not in {"", "/"}:
                raise ValueError("external gateway endpoint must be a dedicated origin without a path prefix")
            if url != self.endpoint and p.scheme != "https" and not self.allow_insecure_upstreams:
                raise ValueError("HTTP upstreams require allow_insecure_upstreams (local integration tests only)")
        for host in [*self.python_artifact_hosts, *self.go_artifact_hosts]:
            if "*" in host or "/" in host:
                raise ValueError("artifact hosts must be exact hostnames")
            NetworkPolicy.validate_hosts([host])
        return self


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
