from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DockerImageStrategy(str, Enum):
    REMOTE = "remote"
    LOCAL_BUILD = "local-build"
    LOCAL_EXISTING = "local-existing"


@dataclass(frozen=True)
class ResolvedDockerImageConfig:
    strategy: DockerImageStrategy
    client_image_ref: str
    image_source: str
    pull_required: bool
    build_required: bool


def resolve_docker_image_strategy(
    *,
    no_rebuild: bool | None,
    explicit_strategy: DockerImageStrategy | None,
) -> DockerImageStrategy:
    if no_rebuild and explicit_strategy is not None:
        raise ValueError(
            "Cannot combine deprecated no_rebuild with docker_image_strategy. "
            "Use only docker_image_strategy."
        )
    if explicit_strategy is not None:
        return explicit_strategy
    if no_rebuild:
        return DockerImageStrategy.LOCAL_EXISTING
    return DockerImageStrategy.REMOTE


def validate_remote_docker_image_coordinates(
    *,
    strategy: DockerImageStrategy,
    docker_image_namespace: str | None,
    docker_image_tag: str | None,
) -> None:
    if strategy != DockerImageStrategy.REMOTE:
        return
    if docker_image_namespace and docker_image_tag:
        return
    raise ValueError(
        "docker_image_namespace and docker_image_tag are required when "
        "docker_image_strategy=remote"
    )


def normalize_task_id(task_id: str) -> str:
    return task_id.lower().replace("_", "-")


def local_client_image_name(task_id: str) -> str:
    return f"lhb__{task_id}__client".lower()


def task_image_repo(namespace: str, task_id: str) -> str:
    return f"{namespace}/lhb-{normalize_task_id(task_id)}"


def remote_client_image_ref(namespace: str, task_id: str, tag: str) -> str:
    return f"{task_image_repo(namespace, task_id)}:{tag}"


def resolve_task_docker_image(
    *,
    task_id: str,
    strategy: DockerImageStrategy,
    docker_image_namespace: str | None,
    docker_image_tag: str | None,
) -> ResolvedDockerImageConfig:
    if strategy == DockerImageStrategy.REMOTE:
        validate_remote_docker_image_coordinates(
            strategy=strategy,
            docker_image_namespace=docker_image_namespace,
            docker_image_tag=docker_image_tag,
        )
        return ResolvedDockerImageConfig(
            strategy=strategy,
            client_image_ref=remote_client_image_ref(
                docker_image_namespace or "",
                task_id,
                docker_image_tag or "",
            ),
            image_source="remote",
            pull_required=True,
            build_required=False,
        )
    if strategy == DockerImageStrategy.LOCAL_BUILD:
        return ResolvedDockerImageConfig(
            strategy=strategy,
            client_image_ref=local_client_image_name(task_id),
            image_source="local",
            pull_required=False,
            build_required=True,
        )
    return ResolvedDockerImageConfig(
        strategy=strategy,
        client_image_ref=local_client_image_name(task_id),
        image_source="local",
        pull_required=False,
        build_required=False,
    )
