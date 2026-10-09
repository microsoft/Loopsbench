"""Trusted, stdlib-only snapshot preparation inside the restricted tester.

Installation code is untrusted and runs with exactly the test container's
permissions/network. Invocation and policy come from the host, not the snapshot.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

STATE = Path("/tmp/loopsbench-dependencies")


def runtime_environment(config: dict, role: str, current=None) -> dict[str, str]:
    env = dict(os.environ if current is None else current)
    endpoint = config["endpoint"].rstrip("/") + "/" + role
    ecosystems = config["access"]["ecosystems"]
    if "python" in ecosystems:
        env.update(PIP_CONFIG_FILE="/dev/null", PIP_INDEX_URL=endpoint + "/python/simple/",
                   PIP_EXTRA_INDEX_URL="", PIP_FIND_LINKS="", PIP_NO_INDEX="0",
                   PIP_TRUSTED_HOST=urlsplit(endpoint).hostname,
                   PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_CACHE_DIR="1",
                   PYTHONNOUSERSITE="1")
        if role == "tester":
            env["VIRTUAL_ENV"] = str(STATE / "python")
            env["PATH"] = str(STATE / "python/bin") + ":" + env.get("PATH", os.defpath)
            env.pop("PYTHONHOME", None)
    if "go" in ecosystems:
        env.update(GOPROXY=endpoint + "/go", GOSUMDB="sum.golang.org " + endpoint + "/sumdb",
                   GOPRIVATE="", GONOPROXY="none", GONOSUMDB="none", GOENV="off",
                   GOTOOLCHAIN="local", GOMODCACHE=str(STATE / "gomod"),
                   GOCACHE=str(STATE / "gobuild"))
    return env


def snapshot_path(workspace: Path, relative: str) -> Path:
    path = (workspace / relative).resolve()
    if not path.is_relative_to(workspace.resolve()):
        raise ValueError("dependency manifest/project symlink escapes snapshot")
    return path


def prepare(config: dict, workspace: Path):
    if STATE.is_symlink():
        STATE.unlink()
    elif STATE.exists():
        shutil.rmtree(STATE)
    STATE.mkdir(parents=True, mode=0o700)
    access = config["access"]
    env = runtime_environment(config, "tester")

    def run(argv, cwd=workspace, child_env=env):
        subprocess.run(argv, cwd=cwd, env=child_env, check=True)

    if "python" in access["ecosystems"]:
        settings = access["python"]
        run([settings["interpreter"], "-I", "-m", "venv", "--copies", str(STATE / "python")], child_env=os.environ.copy())
        pip = [str(STATE / "python/bin/python"), "-I", "-m", "pip", "install", "--no-input"]
        if settings["project"] is not None:
            # Editable install keeps task code at its submitted location. Build
            # requirements are fetched through the same policy gateway.
            run([*pip, "-e", str(snapshot_path(workspace, settings["project"]))])
        requirements = settings["test_requirements"]
        if requirements:
            run([*pip, *requirements])
        for relative in settings["requirements_files"]:
            run([*pip, "-r", str(snapshot_path(workspace, relative))])
        print("LOOPSBENCH_DEPENDENCY_RESOLUTION python", flush=True)
        run([str(STATE / "python/bin/python"), "-I", "-m", "pip", "list", "--format=json"])
    if "go" in access["ecosystems"]:
        for relative in access["go_modules"]:
            module = snapshot_path(workspace, relative)
            snapshot_path(workspace, str((module / "go.mod").relative_to(workspace)))
            run(["go", "mod", "download", "-json"], cwd=module)
            print("LOOPSBENCH_DEPENDENCY_RESOLUTION go " + relative, flush=True)
            run(["go", "list", "-m", "-json", "all"], cwd=module)


if __name__ == "__main__":
    config = json.loads(Path(sys.argv[1]).read_text())
    if sys.argv[2] == "prepare":
        prepare(config, Path(sys.argv[3]).resolve())
    elif sys.argv[2] == "exec":
        os.execvpe(sys.argv[3], sys.argv[3:], runtime_environment(config, "tester"))
    else:
        raise SystemExit("unknown dependency preparation operation")
