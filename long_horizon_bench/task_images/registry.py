from __future__ import annotations

from pathlib import Path
import re

import yaml
from pydantic import BaseModel

from long_horizon_bench.handlers.trial_handler import Task
from long_horizon_bench.task_images.strategy import (
    local_client_image_name,
    normalize_task_id,
    remote_client_image_ref,
    task_image_repo,
)


_SEG_TASK_RE = re.compile(r"_seg\d+$")


class TaskImageSource(BaseModel):
    task_id: str
    task_dir: Path
    compose_file: Path
    dockerfile_path: Path


class ManifestImageRecord(BaseModel):
    task_id: str
    image_repo: str
    image_tag: str
    image_ref: str
    image_digest: str | None = None
    platform: str
    compose_file: str
    dockerfile_path: str
    source_ref: str
    published_at: str | None = None
    verified: bool = False
    notes: str = ""

def _relative_compose_path(task_dir: Path, task: Task) -> Path:
    compose_file = Path(task.docker.compose_file)
    if compose_file.is_absolute() or compose_file.name == "":
        raise ValueError("docker.compose_file must be a relative path")
    compose_path = (task_dir / compose_file).resolve()
    task_root = task_dir.resolve()
    if task_root not in compose_path.parents and compose_path != task_root:
        raise ValueError("docker.compose_file must stay within the task directory")
    return compose_path


def _client_build_dockerfile(compose_path: Path) -> Path:
    compose_data = yaml.safe_load(compose_path.read_text()) or {}
    services = compose_data.get("services") or {}
    client = services.get("client") or {}
    build = client.get("build")
    if isinstance(build, str):
        context_dir = (compose_path.parent / build).resolve()
        return context_dir / "Dockerfile"
    if isinstance(build, dict):
        context_dir = (compose_path.parent / build.get("context", ".")).resolve()
        dockerfile = Path(build.get("dockerfile", "Dockerfile"))
        return (context_dir / dockerfile).resolve()
    raise ValueError(f"client.build missing in {compose_path}")


def discover_task_images(tasks_root: Path) -> list[TaskImageSource]:
    discovered: list[TaskImageSource] = []
    for task_dir in sorted(tasks_root.iterdir()):
        if not task_dir.is_dir() or not task_dir.name.startswith("task_"):
            continue
        task_yaml = task_dir / "task.yaml"
        if not task_yaml.is_file():
            continue
        task = Task.from_yaml(task_yaml)
        compose_path = _relative_compose_path(task_dir, task)
        if not compose_path.is_file():
            continue
        discovered.append(
            TaskImageSource(
                task_id=task_dir.name,
                task_dir=task_dir.resolve(),
                compose_file=compose_path,
                dockerfile_path=_client_build_dockerfile(compose_path),
            )
        )
    return discovered


def discover_nonseg_task_images(tasks_root: Path) -> list[TaskImageSource]:
    return [
        source
        for source in discover_task_images(tasks_root)
        if not _SEG_TASK_RE.search(source.task_id)
    ]


def build_manifest_records(
    *,
    tasks_root: Path,
    namespace: str,
    tag: str,
    platform: str,
    source_ref: str,
) -> list[ManifestImageRecord]:
    task_root_name = tasks_root.name
    records: list[ManifestImageRecord] = []
    for source in discover_nonseg_task_images(tasks_root):
        image_repo = task_image_repo(namespace, source.task_id)
        records.append(
            ManifestImageRecord(
                task_id=source.task_id,
                image_repo=image_repo,
                image_tag=tag,
                image_ref=f"{image_repo}:{tag}",
                image_digest=None,
                platform=platform,
                compose_file=Path(task_root_name, source.task_id).joinpath(
                    source.compose_file.relative_to(source.task_dir)
                ).as_posix(),
                dockerfile_path=Path(task_root_name, source.task_id).joinpath(
                    source.dockerfile_path.relative_to(source.task_dir)
                ).as_posix(),
                source_ref=source_ref,
                published_at=None,
                verified=False,
                notes="",
            )
        )
    return records


def load_manifest(manifest_path: Path) -> list[ManifestImageRecord]:
    if not manifest_path.exists():
        return []
    data = yaml.safe_load(manifest_path.read_text()) or {}
    images = data.get("images") or []
    return [ManifestImageRecord.model_validate(item) for item in images]


def write_manifest(
    manifest_path: Path,
    records: list[ManifestImageRecord],
) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "images": [record.model_dump(mode="json") for record in records],
    }
    manifest_path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
    )
