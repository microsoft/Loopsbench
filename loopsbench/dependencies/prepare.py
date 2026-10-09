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
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit

STATE = Path("/tmp/loopsbench-dependencies")
CLIENT_STATE = Path("/tmp/loopsbench-client-dependencies")
TOOLS = {"npm": "npm", "npx": "npm", "cargo": "cargo", "mvn": "maven", "gradle": "maven",
         "dotnet": "nuget", "apt": "apt", "apt-get": "apt"}


def state_for(role):
    return STATE if role == "tester" else CLIENT_STATE


def write_config(path, content):
    # Parallel package managers must never observe half-written XML/TOML.
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    temporary.replace(path)


def runtime_environment(config: dict, role: str, current=None) -> dict[str, str]:
    env = dict(os.environ if current is None else current)
    endpoint = config["endpoint"].rstrip("/") + "/" + role
    ecosystems = config["access"]["ecosystems"]
    state = state_for(role)
    env["LOOPSBENCH_DEPENDENCY_ENDPOINT"] = endpoint
    if "python" in ecosystems:
        env.update(PIP_CONFIG_FILE="/dev/null", PIP_INDEX_URL=endpoint + "/python/simple/",
                   PIP_EXTRA_INDEX_URL="", PIP_FIND_LINKS="", PIP_NO_INDEX="0",
                   PIP_TRUSTED_HOST=urlsplit(endpoint).hostname,
                   PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_CACHE_DIR="1",
                   PYTHONNOUSERSITE="1")
        if role == "tester" and (STATE / "python/bin/python").exists():
            env["VIRTUAL_ENV"] = str(STATE / "python")
            env["PATH"] = str(STATE / "python/bin") + ":" + env.get("PATH", os.defpath)
            env.pop("PYTHONHOME", None)
    if "go" in ecosystems:
        env.update(GOPROXY=endpoint + "/go", GOSUMDB="sum.golang.org " + endpoint + "/sumdb",
                   GOPRIVATE="", GONOPROXY="none", GONOSUMDB="none", GOENV="off",
                   GOTOOLCHAIN="local", GOMODCACHE=str(STATE / "gomod"),
                   GOCACHE=str(STATE / "gobuild"))
    if "npm" in ecosystems:
        env.update(NPM_CONFIG_REGISTRY=endpoint + "/npm/", NPM_CONFIG_CACHE=str(state / "npm-cache"),
                   NPM_CONFIG_USERCONFIG=str(state / "npmrc"), NPM_CONFIG_AUDIT="false",
                   NPM_CONFIG_FUND="false", NPM_CONFIG_UPDATE_NOTIFIER="false", NPM_CONFIG_REPLACE_REGISTRY_HOST="npmjs")
    if "cargo" in ecosystems:
        env.update(CARGO_HOME=str(state / "cargo"), CARGO_TARGET_DIR=str(state / "cargo-target"),
                   CARGO_NET_OFFLINE="false")
    if "maven" in ecosystems:
        env.update(GRADLE_USER_HOME=str(state / "gradle"))
    if "nuget" in ecosystems:
        env.update(NUGET_PACKAGES=str(state / "nuget-packages"), NUGET_HTTP_CACHE_PATH=str(state / "nuget-http"),
                   DOTNET_CLI_HOME=str(state / "dotnet"), DOTNET_SKIP_FIRST_TIME_EXPERIENCE="1",
                   DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1")
    return env


def write_configs(config, role):
    """These files configure tools; the network/gateway enforce access policy."""
    state = state_for(role)
    state.mkdir(parents=True, exist_ok=True)
    base = config["endpoint"].rstrip("/") + "/" + role
    # "always" duplicates the gateway path on already-rewritten tarball URLs.
    write_config(state / "npmrc", f"registry={base}/npm/\naudit=false\nfund=false\nreplace-registry-host=npmjs\n")
    (state / "cargo").mkdir(exist_ok=True)
    write_config(state / "cargo/config.toml", '[source.crates-io]\nreplace-with="loopsbench"\n'
                                            f'[source.loopsbench]\nregistry="sparse+{base}/cargo/index/"\n')
    settings = ET.Element("settings")
    ET.SubElement(settings, "localRepository").text = str(state / "maven")
    mirror = ET.SubElement(ET.SubElement(settings, "mirrors"), "mirror")
    for key, value in {"id": "loopsbench", "mirrorOf": "*", "url": base + "/maven/"}.items():
        ET.SubElement(mirror, key).text = value
    write_config(state / "maven-settings.xml", ET.tostring(settings, encoding="unicode"))
    nuget = ET.Element("configuration")
    sources = ET.SubElement(nuget, "packageSources")
    ET.SubElement(sources, "clear")
    ET.SubElement(sources, "add", {"key": "loopsbench", "value": base + "/nuget/index.json", "allowInsecureConnections": "true"})
    write_config(state / "nuget.config", ET.tostring(nuget, encoding="unicode"))
    # beforeSettings also covers plugin resolution during settings evaluation.
    repository = "maven { url = uri(" + json.dumps(base + "/maven/") + "); allowInsecureProtocol = true }"
    write_config(state / "gradle.init.gradle",
        'beforeSettings { s -> s.pluginManagement.repositories { clear(); ' + repository + ' } }\n'
        'settingsEvaluated { s -> s.dependencyResolutionManagement.repositories { clear(); ' + repository + ' } }\n'
        'allprojects { p ->\n'
        ' p.buildscript.repositories { clear(); ' + repository + ' }\n'
        ' p.repositories { clear(); ' + repository + ' }\n'
        ' p.afterEvaluate { p.repositories { clear(); ' + repository + ' } }\n}\n')
    lines = []
    for name, repo in config.get("apt_repositories", {}).items():
        for suite in repo["suites"]:
            lines.append(f'deb [arch={",".join(repo["architectures"])} signed-by={repo["signed_by"]} by-hash=no] '
                         f'{base}/apt/{name} {suite} {" ".join(repo["components"])}')
    write_config(state / "apt.sources.list", "\n".join(lines) + "\n")
    (state / "apt-lists/partial").mkdir(parents=True, exist_ok=True)


def tool_argv(tool, args, role):
    state = state_for(role)
    if tool in {"npm", "npx"}:
        # Registry env alone loses to project .npmrc. An explicit option also
        # rewrites default-registry lock URLs while retaining lock integrity.
        return [tool, "--userconfig=" + str(state / "npmrc"), *args]
    if tool == "cargo":
        return [tool, "--config", str(state / "cargo/config.toml"), *args]
    if tool == "mvn":
        return [tool, "--settings", str(state / "maven-settings.xml"), "-Dmaven.repo.local=" + str(state / "maven"), *args]
    if tool == "gradle":
        return [tool, "--no-daemon", "--init-script", str(state / "gradle.init.gradle"), *args]
    if tool == "dotnet" and args and args[0] in {"restore", "build", "test", "pack", "publish"}:
        return [tool, *args, "-p:RestoreConfigFile=" + str(state / "nuget.config"), "-p:NuGetAudit=false"]
    if tool in {"apt", "apt-get"}:
        options = {"Dir::Etc::sourcelist": str(state / "apt.sources.list"), "Dir::Etc::sourceparts": "-",
                   "Dir::State::lists": str(state / "apt-lists"), "Acquire::Languages": "none",
                   "Acquire::PDiffs": "false", "Acquire::By-Hash": "false"}
        return [tool, *(value for key, val in options.items() for value in ("-o", key + "=" + val)), *args]
    return [tool, *args]


def exec_tool(config, role, tool, args):
    # Excluding the wrapper directory avoids recursion and keeps rustup's cargo
    # symlink name intact. No changes to agents' invocation APIs are required.
    env = os.environ.copy()
    wrapper_bin = config["wrapper_bin"]
    env["PATH"] = ":".join(p for p in env.get("PATH", os.defpath).split(":") if p != wrapper_bin)
    program = shutil.which(tool, path=env["PATH"])
    if not program:
        raise SystemExit("task image has no " + tool)
    solving = role == "tester" or Path(config["phase_file"]).read_text().strip() == "solving"
    if solving:
        write_configs(config, role)
        env = runtime_environment(config, role, env)
        argv = tool_argv(tool, args, role)
    else:
        argv = [tool, *args]
    # Nested package-manager commands should see the wrappers too.
    env["PATH"] = wrapper_bin + ":" + env["PATH"]
    os.execve(program, argv, env)


def snapshot_path(workspace: Path, relative: str) -> Path:
    path = (workspace / relative).resolve()
    if not path.is_relative_to(workspace.resolve()):
        raise ValueError("dependency manifest/project symlink escapes snapshot")
    return path


def project_paths(access, plugin):
    return access.get("projects", {}).get(plugin, access["go_modules"] if plugin == "go" else ["."])


def preparation_plugins(config, workspace):
    access = config["access"]
    if access.get("prepare") is not None:
        return access["prepare"]
    markers = {"python": ("pyproject.toml", "setup.py", "setup.cfg"), "go": ("go.mod",),
               "npm": ("package.json",), "cargo": ("Cargo.toml",), "maven": ("pom.xml",),
               "gradle": ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"),
               "nuget": ("*.csproj", "*.fsproj", "*.vbproj", "*.sln", "*.slnx")}
    selected = []
    for plugin, patterns in markers.items():
        if ("maven" if plugin == "gradle" else plugin) not in access["ecosystems"]:
            continue
        if plugin == "python":
            if access["python"]["test_requirements"] or access["python"]["requirements_files"]:
                selected.append(plugin)
                continue
            paths = [access["python"]["project"]] if access["python"]["project"] is not None else []
        else:
            paths = project_paths(access, plugin)
        if any(any(snapshot_path(workspace, path).glob(pattern)) for path in paths for pattern in patterns):
            selected.append(plugin)
    return selected


def remove_state(path):
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def prepare(config: dict, workspace: Path):
    remove_state(STATE)
    STATE.mkdir(parents=True, mode=0o700)
    access = config["access"]
    selected = preparation_plugins(config, workspace)
    write_configs(config, "tester")
    env = runtime_environment(config, "tester")

    def run(argv, cwd=workspace, child_env=None):
        subprocess.run(argv, cwd=cwd, env=env if child_env is None else child_env, check=True)

    if "python" in selected:
        settings = access["python"]
        run([settings["interpreter"], "-I", "-m", "venv", "--copies", str(STATE / "python")], child_env=os.environ.copy())
        env = runtime_environment(config, "tester")
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
    if "go" in selected:
        for relative in project_paths(access, "go"):
            module = snapshot_path(workspace, relative)
            snapshot_path(workspace, str((module / "go.mod").relative_to(workspace)))
            run(["go", "mod", "download", "-json"], cwd=module)
            print("LOOPSBENCH_DEPENDENCY_RESOLUTION go " + relative, flush=True)
            run(["go", "list", "-m", "-json", "all"], cwd=module)
    for plugin in selected:
        if plugin in {"python", "go"}:
            continue
        for relative in project_paths(access, plugin):
            project = snapshot_path(workspace, relative)
            print("LOOPSBENCH_DEPENDENCY_RESOLUTION " + plugin + " " + relative, flush=True)
            if plugin == "npm":
                remove_state(project / "node_modules")
                run(["npm", "ci" if (project / "package-lock.json").exists() or (project / "npm-shrinkwrap.json").exists() else "install",
                     "--no-audit", "--no-fund"], cwd=project)
                run(["npm", "ls", "--all", "--json"], cwd=project)
            elif plugin == "cargo":
                snapshot_path(workspace, str((project / "Cargo.toml").relative_to(workspace)))
                run(["cargo", "fetch", "--manifest-path", str(project / "Cargo.toml")], cwd=project)
                run(["cargo", "metadata", "--format-version=1"], cwd=project)
            elif plugin == "maven":
                pom = snapshot_path(workspace, str((project / "pom.xml").relative_to(workspace)))
                for manifest in project.rglob("pom.xml"):
                    directory = snapshot_path(workspace, str(manifest.parent.relative_to(workspace)))
                    remove_state(directory / "target")
                reactor = any(element.tag.rsplit("}", 1)[-1] == "modules" and len(element) for element in ET.parse(pom).iter())
                if reactor:
                    # Sibling artifacts are submitted code. Build/install them
                    # locally so dependency:resolve cannot try to retrieve a
                    # published target module before the reactor has built it.
                    run(["mvn", "-B", "-Dmaven.test.skip=true", "install"], cwd=project)
                else:
                    run(["mvn", "-B", "dependency:resolve", "-DincludeScope=test"], cwd=project)
            elif plugin == "gradle":
                for manifest in [*project.rglob("build.gradle"), *project.rglob("build.gradle.kts")]:
                    directory = snapshot_path(workspace, str(manifest.parent.relative_to(workspace)))
                    remove_state(directory / "build")
                    remove_state(directory / ".gradle")
                run(["gradle", "dependencies"], cwd=project)
            elif plugin == "nuget":
                for pattern in ("*.csproj", "*.fsproj", "*.vbproj"):
                    for manifest in project.rglob(pattern):
                        directory = snapshot_path(workspace, str(manifest.parent.relative_to(workspace)))
                        remove_state(directory / "bin")
                        remove_state(directory / "obj")
                run(["dotnet", "restore"], cwd=project)


if __name__ == "__main__":
    config = json.loads(Path(sys.argv[1]).read_text())
    if sys.argv[2] == "prepare":
        prepare(config, Path(sys.argv[3]).resolve())
    elif sys.argv[2] == "exec":
        os.execvpe(sys.argv[3], sys.argv[3:], runtime_environment(config, "tester"))
    elif sys.argv[2] == "tool":
        exec_tool(config, sys.argv[3], sys.argv[4], sys.argv[5:])
    else:
        raise SystemExit("unknown dependency preparation operation")
