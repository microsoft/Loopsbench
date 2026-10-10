from __future__ import annotations

import logging

import pytest
import yaml

from loopsbench.agents.agent_name import AgentName
from loopsbench.dependencies.config import DependencyService
from loopsbench.file_config import load_run_file
from loopsbench.handlers.trial_handler import Task, TrialHandler
from loopsbench.harness.harness import Harness
from loopsbench.task_images.strategy import DockerImageStrategy
from loopsbench.terminal.network_policy import NetworkPlan

REPOSITORIES = ["https://repo.jenkins-ci.org/public"]
ARTIFACT_HOSTS = ["repo.jenkins-ci.org"]


@pytest.mark.parametrize(
    "run_settings",
    [
        None,
        {"provider": "docker"},
        {"max_concurrent_downloads": 2},
        {"maven_repository_urls": DependencyService().maven_repository_urls},
        {"maven_artifact_hosts": []},
    ],
)
def test_harness_uses_task_service_defaults_without_cross_task_leaks(
    tmp_path, monkeypatch, run_settings,
):
    run_path = tmp_path / "run.yaml"
    run_path.write_text(yaml.safe_dump({"dependency_service": run_settings}))
    run = load_run_file(run_path)
    harness = Harness.__new__(Harness)
    harness._network = NetworkPlan()
    harness._dependency_service = DependencyService.model_validate(run.dependency_service or {})
    harness._task_dependency_overrides = {}
    harness._logger = logging.getLogger("test")
    harness._agent_name = AgentName.ORACLE
    harness._global_agent_timeout_sec = None
    harness._global_timeout_multiplier = 1.0
    harness._docker_image_strategy = DockerImageStrategy.LOCAL_EXISTING
    harness._docker_image_namespace = None
    harness._docker_image_tag = None
    harness._no_rebuild = False
    harness._cleanup = True
    captured = []

    class CapturedSettings(Exception):
        pass

    def capture(**kwargs):
        captured.append(kwargs)
        raise CapturedSettings

    monkeypatch.setattr("loopsbench.harness.harness.spin_up_terminal", capture)
    for task_id, config in [
        ("task_jenkins", {
            "dependency_access": {
                "ecosystems": ["maven"],
                "blocked_packages": {"maven": ["org.jenkins-ci.main:jenkins-core"]},
                "prepare": [],
            },
            "dependency_service": {
                "maven_repository_urls": REPOSITORIES,
                "maven_artifact_hosts": ARTIFACT_HOSTS,
            },
        }),
        ("task_offline", {}),
    ]:
        task_dir = tmp_path / task_id
        task_dir.mkdir()
        (task_dir / "task.yaml").write_text(yaml.safe_dump({"instruction": "test", **config}))
        (task_dir / "docker-compose.yaml").write_text("services: {}\n")
        handler = TrialHandler(
            trial_name=f"{task_id}.1-of-1.test",
            input_path=task_dir,
            output_path=tmp_path / "runs",
        )
        with pytest.raises(CapturedSettings):
            harness._run_trial(handler)

    effective = captured[0]["dependency_service"]
    explicit = run_settings or {}
    assert effective.maven_repository_urls == explicit.get("maven_repository_urls", REPOSITORIES)
    assert effective.maven_artifact_hosts == explicit.get("maven_artifact_hosts", ARTIFACT_HOSTS)
    assert effective.max_concurrent_downloads == explicit.get("max_concurrent_downloads", 4)
    assert captured[0]["dependency_access"].blocked_packages == {
        "maven": ["org.jenkins-ci.main:jenkins-core"],
    }
    assert captured[1]["dependency_service"] == DependencyService.model_validate(explicit)
    assert captured[1]["dependency_access"] is None
    assert harness._dependency_service.model_dump(exclude_unset=True) == explicit


def test_task_service_alone_does_not_enable_gateway():
    task = Task(instruction="test", dependency_service={"maven_repository_urls": REPOSITORIES})
    assert task.dependency_access is None


@pytest.mark.parametrize("service", [
    {"maven_artifact_hosts": ["*.example.org"]},
    {"maven_repository_urls": ["https://user:password@example.org/maven"]},
    {"maven_repository_url": "https://example.org/maven"},
])
def test_task_service_validates_hosts_urls_and_unknown_fields(service):
    with pytest.raises(ValueError):
        Task(instruction="test", dependency_service=service)
