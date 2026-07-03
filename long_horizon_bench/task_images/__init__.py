from __future__ import annotations

from .strategy import (
    DockerImageStrategy,
    ResolvedDockerImageConfig,
    local_client_image_name,
    normalize_task_id,
    remote_client_image_ref,
    resolve_docker_image_strategy,
    resolve_task_docker_image,
    task_image_repo,
    validate_remote_docker_image_coordinates,
)

__all__ = [
    "DockerImageStrategy",
    "ManifestImageRecord",
    "TaskImageSource",
    "ResolvedDockerImageConfig",
    "build_manifest_records",
    "discover_task_images",
    "discover_nonseg_task_images",
    "load_manifest",
    "local_client_image_name",
    "normalize_task_id",
    "remote_client_image_ref",
    "resolve_docker_image_strategy",
    "resolve_task_docker_image",
    "task_image_repo",
    "validate_remote_docker_image_coordinates",
    "write_manifest",
]


def __getattr__(name: str):
    if name in {
        "ManifestImageRecord",
        "TaskImageSource",
        "build_manifest_records",
        "discover_task_images",
        "discover_nonseg_task_images",
        "load_manifest",
        "write_manifest",
    }:
        from . import registry

        return getattr(registry, name)
    raise AttributeError(name)
