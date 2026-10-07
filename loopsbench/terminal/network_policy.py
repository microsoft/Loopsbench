"""Harbor-style phase policies, independent of task images and agent protocols."""

from __future__ import annotations

import copy
import ipaddress
import os
import re
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NetworkMode = Literal["public", "no-network", "allowlist"]

# Read only runner-provided inference settings. Repository URLs, download
# mirrors, search/MCP servers and generic HTTP proxies are not model endpoints.
MODEL_URL_ENV_KEYS = frozenset({
    "OPENAI_BASE_URL", "OPENAI_API_BASE", "ANTHROPIC_BASE_URL",
    "AZURE_API_BASE", "AZURE_OPENAI_ENDPOINT", "LLM_BASE_URL", "LLM_API_BASE",
    "MSWEA_API_BASE", "MSWEA_BASE_URL", "PROXY_BASE", "COPILOT_SHIM_BASE_URL",
    "GITHUB_COPILOT_API_BASE", "COPILOT_LOOPSBENCH_PROVIDER_BASE_URL",
    "COPILOT_PROVIDER_BASE_URL", "DASHSCOPE_BASE_URL", "DASHSCOPE_OPENAI_BASE_URL",
    "GEMINI_API_BASE", "GOOGLE_GEMINI_BASE_URL", "OLLAMA_HOST", "OLLAMA_API_BASE",
    "OPENROUTER_API_BASE", "TOGETHERAI_API_BASE", "DEEPSEEK_API_BASE",
})
MODEL_URL_KWARG_KEYS = frozenset({
    "url", "base_url", "api_base", "api_base_url", "api_url", "endpoint",
    "azure_endpoint", "model_url", "model_base_url", "provider_base_url",
    "llm_base_url", "openai_base_url", "anthropic_base_url",
})
MODEL_CONFIG_KEYS = frozenset({"model_kwargs", "llm_kwargs", "model_config", "llm_config", "llm"})


def model_endpoint_hosts(urls: list[str]) -> list[str]:
    """Extract exact hosts without persisting URL credentials, paths or keys."""
    hosts = []
    for url in urls:
        if not url or not url.strip():
            continue
        try:
            if any(ord(char) < 32 for char in url):
                raise ValueError
            parsed = urlsplit(url.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError
            # Accessing port also validates malformed authorities.
            _ = parsed.port
            host = parsed.hostname.encode("idna").decode("ascii")
            if "*" in host or "/" in host:
                raise ValueError
            hosts.extend(NetworkPolicy.validate_hosts([host]))
        except (ValueError, UnicodeError):
            # Never echo a credential-bearing URL in a validation error.
            raise ValueError("Model endpoint must be an HTTP(S) URL with a valid hostname/IP and port") from None
    return list(dict.fromkeys(hosts))


def configured_model_hosts(
    agent_kwargs: Mapping[str, Any] | None = None,
    environment: Mapping[str, str] | None = None,
) -> list[str]:
    """Discover model URLs in trusted run kwargs and model/runtime env files."""
    environment = os.environ if environment is None else environment
    urls = [environment[key] for key in sorted(MODEL_URL_ENV_KEYS) if environment.get(key)]

    def collect(values: Mapping[str, Any]) -> None:
        for key, value in values.items():
            if key in MODEL_URL_KWARG_KEYS and isinstance(value, str):
                urls.append(value)
            elif key in MODEL_CONFIG_KEYS and isinstance(value, Mapping):
                collect(value)
            elif key == "env" and isinstance(value, Mapping):
                urls.extend(value[name] for name in sorted(MODEL_URL_ENV_KEYS) if isinstance(value.get(name), str))
            elif key in {"providers", "model_providers"} and isinstance(value, Mapping):
                for provider in value.values():
                    if isinstance(provider, Mapping):
                        collect(provider)

    collect(agent_kwargs or {})
    return model_endpoint_hosts(urls)


class NetworkPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    network_mode: NetworkMode = "public"
    allowed_hosts: list[str] = Field(default_factory=list)

    @field_validator("allowed_hosts")
    @classmethod
    def validate_hosts(cls, hosts: list[str]) -> list[str]:
        result = []
        for host in hosts:
            host = host.strip().lower().rstrip(".")
            try:
                host = str(ipaddress.ip_network(host, strict=False)) if "/" in host else str(ipaddress.ip_address(host))
            except ValueError:
                domain = host.removeprefix("*.")
                if len(domain) > 253 or not all(
                    re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                    for part in domain.split(".")
                ):
                    raise ValueError("allowed_hosts must contain hostnames, IPs or CIDRs, not URLs")
            if host not in result:
                result.append(host)
        return result

    @model_validator(mode="after")
    def validate_mode(self):
        if self.allowed_hosts and self.network_mode != "allowlist":
            raise ValueError("allowed_hosts requires network_mode=allowlist")
        return self

    def command(self) -> list[str]:
        if self.network_mode == "public":
            return ["network-policy", "allow-all"]
        if self.network_mode == "no-network":
            return ["network-policy", "deny-all"]
        return ["network-policy", "allow", *self.allowed_hosts]


class NetworkPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: NetworkPolicy = Field(default_factory=NetworkPolicy)
    agent: NetworkPolicy = Field(default_factory=lambda: NetworkPolicy(network_mode="allowlist"))
    verifier: NetworkPolicy = Field(default_factory=lambda: NetworkPolicy(network_mode="no-network"))
    extra_allowed_hosts: list[str] = Field(default_factory=list)

    _validate_hosts = field_validator("extra_allowed_hosts")(NetworkPolicy.validate_hosts.__func__)

    @classmethod
    def from_config(cls, value: NetworkPlan | dict | None, *, agent_kwargs: Mapping[str, Any] | None = None) -> NetworkPlan:
        plan = value if isinstance(value, cls) else cls.model_validate(value or {})
        extra = os.environ.get("LOOPSBENCH_ALLOWED_HOSTS", "")
        hosts = NetworkPolicy.validate_hosts([
            *plan.extra_allowed_hosts, *filter(None, extra.split(",")),
            *configured_model_hosts(agent_kwargs),
        ])
        return plan.model_copy(update={"extra_allowed_hosts": hosts})

    def agent_policy(self, model_hosts: list[str] | None = None) -> NetworkPolicy:
        if self.agent.network_mode != "allowlist":
            return self.agent
        return NetworkPolicy(network_mode="allowlist", allowed_hosts=[*self.agent.allowed_hosts, *self.extra_allowed_hosts, *(model_hosts or [])])


def controlled_compose(document: dict, plan: NetworkPlan, image: str) -> tuple[dict, dict[str, str]]:
    """Transform resolved Compose, preserving task commands, files and build paths."""
    document = copy.deepcopy(document)
    services = document["services"]
    controllers = {}
    for name in ("client", "tester"):
        if name not in services:
            continue
        initial = plan.environment
        phases = [initial, plan.agent_policy() if name == "client" else plan.verifier]
        if all(policy.network_mode == "public" for policy in phases):
            continue
        service = services[name]
        mode = service.get("network_mode")
        if mode not in (None, "bridge", "none"):
            raise ValueError(f"Cannot apply restricted network policy to {name} with network_mode={mode!r}")
        if (service.get("privileged") or service.get("pid") == "host"
                or {str(cap).upper() for cap in service.get("cap_add", [])} & {"ALL", "NET_ADMIN", "NET_RAW", "SYS_ADMIN"}
                or service.get("use_api_socket")):
            raise ValueError(f"Restricted network policy is incompatible with privileged service {name}")
        sidecar = f"loopsbench-egress-{name}"
        if sidecar in services:
            raise ValueError(f"Reserved harness service name: {sidecar}")
        controller = {
            "image": image,
            "entrypoint": ["/opt/egress-sidecar/entrypoint.sh"],
            "init": True,
            "cap_add": ["NET_ADMIN", "NET_RAW"],
            "environment": {"EGRESS_CONTROL_INITIAL_NETWORK_MODE": initial.network_mode,
                            "EGRESS_CONTROL_INITIAL_ALLOWED_HOSTS": " ".join(initial.allowed_hosts)},
            "healthcheck": {"test": ["CMD", "test", "-f", "/tmp/egress.ready"],
                            "interval": "1s", "timeout": "5s", "retries": 30},
        }
        # Docker disallows these on containers that share a network namespace.
        # Move them to its owner. Application mounts and entrypoints stay intact.
        for key in ("networks", "ports", "expose", "hostname", "domainname", "dns", "dns_search", "dns_opt", "extra_hosts", "mac_address"):
            if key in service:
                controller[key] = service.pop(key)
        networks = controller.setdefault("networks", {"default": {}})
        if isinstance(networks, list):
            networks = controller["networks"] = {network: {} for network in networks}
        if "default" in networks:
            document.setdefault("networks", {}).setdefault("default", {})
        for network, settings in list(networks.items()):
            settings = networks[network] = settings or {}
            settings["aliases"] = list(dict.fromkeys([*settings.get("aliases", []), name]))
        service["network_mode"] = f"service:{sidecar}"
        service["cap_drop"] = sorted(set(service.get("cap_drop", [])) | {"NET_ADMIN", "NET_RAW", "SYS_ADMIN"})
        service["security_opt"] = list(dict.fromkeys([*service.get("security_opt", []), "no-new-privileges:true"]))
        dependencies = service.setdefault("depends_on", {})
        if isinstance(dependencies, list):
            dependencies = service["depends_on"] = {item: {"condition": "service_started"} for item in dependencies}
        dependencies[sidecar] = {"condition": "service_healthy"}
        services[sidecar] = controller
        controllers[name] = sidecar
    return document, controllers
