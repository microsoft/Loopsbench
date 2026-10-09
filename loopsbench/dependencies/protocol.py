"""Small stdlib-only validation shared with the gateway container."""
from __future__ import annotations

import re


def canonical_package(ecosystem: str, value: str) -> str:
    if ecosystem == "python":
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", value):
            raise ValueError("invalid Python package name")
        return re.sub(r"[-_.]+", "-", value).lower()
    if ecosystem == "go":
        # Includes Go proxy's !lowercase escaping; reject noncanonical escapes.
        value = re.sub(r"!([a-z])", lambda m: m[1].upper(), value)
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~/-]*", value)
                or any(part in {"", ".", ".."} for part in value.split("/"))):
            raise ValueError("invalid Go module path")
        return value
    raise ValueError("unsupported dependency ecosystem")


def check_package(policy: dict, ecosystem: str, name: str) -> str:
    name = canonical_package(ecosystem, name)
    if ecosystem not in policy["ecosystems"]:
        raise PermissionError("ecosystem is not enabled")
    blocked = policy["blocked_packages"].get(ecosystem, [])
    if ecosystem == "go":
        blocked = [*blocked, "golang.org/toolchain"]
    for item in blocked:
        item = canonical_package(ecosystem, item)
        compared_name, compared_item = name, item
        if ecosystem == "go":
            # Go encodes uppercase letters with !, while GitHub repository
            # identities are case-insensitive and may carry a .git suffix.
            # Compare conservatively so a spelling change cannot expose the
            # blocked target's zip via the module proxy.
            compared_name = "/".join(part.removesuffix(".git") for part in name.lower().split("/"))
            compared_item = "/".join(part.removesuffix(".git") for part in item.lower().split("/"))
        if compared_name == compared_item or (ecosystem == "go" and compared_name.startswith(compared_item + "/")):
            raise PermissionError("package is blocked by task policy")
    return name
