"""Harbor-style phase policies, independent of task images and agent protocols."""

from __future__ import annotations

import copy
import ipaddress
import os
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NetworkMode = Literal["public", "no-network", "allowlist"]


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
    def from_config(cls, value: NetworkPlan | dict | None) -> NetworkPlan:
        plan = value if isinstance(value, cls) else cls.model_validate(value or {})
        extra = os.environ.get("LOOPSBENCH_ALLOWED_HOSTS", "")
        hosts = NetworkPolicy.validate_hosts([*plan.extra_allowed_hosts, *filter(None, extra.split(","))])
        return plan.model_copy(update={"extra_allowed_hosts": hosts})

    def agent_policy(self) -> NetworkPolicy:
        if self.agent.network_mode != "allowlist":
            return self.agent
        return NetworkPolicy(network_mode="allowlist", allowed_hosts=[*self.agent.allowed_hosts, *self.extra_allowed_hosts])


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
