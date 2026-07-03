"""Qwen Code CLI integration for Long-Horizon-Bench.

**Default:** run ``qwen`` **inside the client container** via ``docker exec``,
``workdir`` typically ``/workspace``. If the image has no CLI yet, the harness
**builds a bundle on the host** (``npm install -g --prefix …``) and uses
``put_archive`` to place it under ``/opt/lhb-qwen-code``. Images without Node get a
**bundled official Node.js** tarball (linux-x64 / linux-arm64) downloaded on
the host and packed next to the npm prefix — **no ``npm`` inside the container**
is required.

**Opt-in legacy mode:** ``QWEN_CODE_WORKSPACE_MODE=mirror`` keeps the host
mirror (extract ``/workspace``, run host ``qwen``, ``put_archive`` back).

Environment:

* ``QWEN_CODE_WORKSPACE_MODE``: ``container`` (default) or ``mirror``.
* ``QWEN_CODE_INSTALL_PREFIX``: install directory in the container (default
  ``/opt/lhb-qwen-code``); ``qwen`` at ``$PREFIX/bin/qwen``.
* ``QWEN_CODE_NPM_PACKAGE``: npm package spec (default ``@qwen-code/qwen-code``).
* ``QWEN_CODE_INSTALL_TIMEOUT_SEC``: host ``npm install -g`` timeout (default
  ``900``).
* ``QWEN_CODE_BUNDLE_CACHE``: host cache dir (default ``~/.cache/lhb-qwen-bundle``).
* ``QWEN_CODE_NODE_DIST_VERSION``: Node.js version for bundled runtime (default
  ``20.18.1``). Ignored when the container already has ``node`` on ``PATH``.
* ``QWEN_CODE_BUNDLE_NODE=0``: never download Node; require ``node`` in the
  container (default is to bundle when ``node`` is missing).
* ``QWEN_CODE_SKIP_BOOTSTRAP``: if set, do not push a bundle; require existing
  ``qwen`` in the container.
* ``NPM_CONFIG_REGISTRY``, ``NODE_AUTH_TOKEN``, proxy vars: used for **host**
  ``npm install``.

Host requirements for container mode: **``npm`` on the harness host** (Node
install toolchain). Mirror mode additionally needs host ``qwen``.

Trajectory: ``stream-json`` on stdout → ``agent-logs/trajectories/qwen_code_stream.jsonl``.

Credentials: passed into container ``exec`` via Docker ``environment``.

Round timeout: mirror uses ``subprocess.run(..., timeout=…)``; container mode
uses stream read deadline and may return ``agent_timeout``.
"""

from __future__ import annotations

import io
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from long_horizon_bench.agents.agent_name import AgentName
from long_horizon_bench.agents.base_agent import AgentResult, BaseAgent
from long_horizon_bench.harness.models import FailureMode
from long_horizon_bench.terminal.tmux_session import TmuxSession

_TRAJECTORY_REL = Path("trajectories") / "qwen_code_stream.jsonl"

_DEFAULT_DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_INTL_DASHSCOPE_BASE = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"


def _strip(s: str | None) -> str:
    return (s or "").strip()


def _dashscope_base_url_from_env(env: dict[str, str]) -> str:
    """Pick DashScope OpenAI-compatible base URL from host env (same shell as lhb)."""
    for key in ("OPENAI_BASE_URL", "DASHSCOPE_OPENAI_BASE_URL", "DASHSCOPE_BASE_URL"):
        v = _strip(env.get(key))
        if v:
            return v
    region = _strip(env.get("DASHSCOPE_REGION")).lower()
    if region in ("intl", "sg", "singapore", "international", "ap-southeast-1"):
        return _INTL_DASHSCOPE_BASE
    return _DEFAULT_DASHSCOPE_BASE


def _openai_api_key_from_env(env: dict[str, str]) -> str:
    """Resolve API key from common terminal exports (DashScope first)."""
    for key in ("DASHSCOPE_API_KEY", "BAILIAN_API_KEY", "OPENAI_API_KEY"):
        v = _strip(env.get(key))
        if v:
            return v
    return ""


def _prepare_qwen_child_env_and_cli_flags(env: dict[str, str]) -> tuple[list[str], str | None]:
    """Return extra ``qwen`` argv; mutates ``env`` in place for the child process."""
    api_key = _openai_api_key_from_env(env)
    base_url = _dashscope_base_url_from_env(env)
    extra: list[str] = ["--auth-type", "openai", "--openai-base-url", base_url]

    if api_key:
        env["OPENAI_API_KEY"] = api_key
    else:
        env.pop("OPENAI_API_KEY", None)

    env["OPENAI_BASE_URL"] = base_url
    return extra, api_key or None


def _resolve_qwen_executable() -> Path:
    env_bin = os.environ.get("QWEN_CODE_BIN")
    if env_bin:
        p = Path(env_bin).expanduser()
        if p.is_file():
            return p
        raise FileNotFoundError(
            f"QWEN_CODE_BIN is set to {env_bin!r} but that file does not exist."
        )
    which = shutil.which("qwen")
    if which:
        return Path(which)
    fallback = Path.home() / ".local" / "bin" / "qwen"
    if fallback.is_file():
        return fallback
    raise FileNotFoundError(
        "Could not find the `qwen` executable. Install Qwen Code "
        "(e.g. `npm install -g @qwen-code/qwen-code --prefix ~/.local`) "
        "or set QWEN_CODE_BIN to the cli.js / launcher path."
    )


_INSTRUCTION_CONTAINER_PATH = "/tmp/lhb_qwen_instruction.txt"


def _workspace_mode() -> str:
    raw = (os.environ.get("QWEN_CODE_WORKSPACE_MODE") or "container").strip().lower()
    if raw in ("container", "mirror"):
        return raw
    raise ValueError(
        "QWEN_CODE_WORKSPACE_MODE must be one of: container, mirror "
        f"(got {raw!r})."
    )


def _truthy_env(name: str) -> bool:
    return _strip(os.environ.get(name)).lower() in ("1", "true", "yes", "on")


def _falsy_env(name: str) -> bool:
    return _strip(os.environ.get(name)).lower() in ("0", "false", "no", "off")


def _bundle_cache_root() -> Path:
    raw = _strip(os.environ.get("QWEN_CODE_BUNDLE_CACHE"))
    return Path(raw).expanduser() if raw else Path.home() / ".cache" / "lhb-qwen-bundle"


def _container_uname_m(container: object) -> str:
    res = container.exec_run(["uname", "-m"], demux=True)
    if int(res.exit_code) != 0:
        return "x86_64"
    return (res.output[0] or b"x86_64").decode("utf-8", errors="replace").strip() or "x86_64"


def _nodejs_dist_triplet(machine: str) -> str:
    m = machine.lower()
    if m in ("x86_64", "amd64"):
        return "linux-x64"
    if m in ("aarch64", "arm64"):
        return "linux-arm64"
    raise RuntimeError(
        f"Bundled Node.js: unsupported container architecture uname -m={machine!r} "
        "(need x86_64/amd64 or aarch64/arm64)."
    )


def _container_has_node(container: object) -> bool:
    res = container.exec_run(["bash", "-lc", "command -v node >/dev/null 2>&1"], demux=True)
    return int(res.exit_code) == 0


def _host_npm_subprocess_env(env: dict[str, str]) -> dict[str, str]:
    """Env for host-side ``npm`` (registry tokens, proxy)."""
    out = os.environ.copy()
    for k in (
        "NPM_CONFIG_REGISTRY",
        "npm_config_registry",
        "NODE_AUTH_TOKEN",
        "NPM_TOKEN",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
    ):
        v = _strip(env.get(k))
        if v:
            out[k] = v
    cache = out.get("NPM_CONFIG_CACHE") or str(Path.home() / ".npm" / "_lhb-qwen-cache")
    out["NPM_CONFIG_CACHE"] = cache
    out.setdefault("npm_config_update_notifier", "false")
    return out


def _bundle_slug(npm_pkg: str, triplet: str, need_node: bool, node_ver: str) -> str:
    raw = f"{npm_pkg}__{triplet}__{'node' + node_ver if need_node else 'sysnode'}"
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "_", raw)
    return slug[:180]


def _download_file(url: str, dest: Path, log_f, timeout_sec: float = 600.0) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log_f.write(f"[bootstrap] downloading {url} → {dest}\n")
    log_f.flush()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "long-horizon-bench-qwen-bundle"})
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:  # noqa: S310
            data = resp.read()
        tmp.write_bytes(data)
        tmp.replace(dest)
    except (urllib.error.URLError, OSError) as e:
        if tmp.is_file():
            tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Download failed: {url!r}: {e}") from e


def _ensure_node_tarball(triplet: str, version: str, cache_dir: Path, log_f) -> Path:
    fname = f"node-v{version}-{triplet}.tar.xz"
    dest = cache_dir / fname
    if dest.is_file() and dest.stat().st_size > 5_000_000:
        return dest
    url = f"https://nodejs.org/dist/v{version}/{fname}"
    _download_file(url, dest, log_f)
    return dest


def _extract_node_root(xz_path: Path, extract_into: Path, log_f) -> Path:
    extract_into.mkdir(parents=True, exist_ok=True)
    with tarfile.open(xz_path, mode="r:xz") as tar:
        tar.extractall(extract_into, filter="data")
    # Expect exactly one top-level directory node-vX-…
    dirs = [p for p in extract_into.iterdir() if p.is_dir()]
    if len(dirs) != 1:
        raise RuntimeError(
            f"Unexpected Node.js tarball layout under {extract_into} "
            f"(expected one root dir, got {[p.name for p in dirs]})."
        )
    log_f.write(f"[bootstrap] extracted Node runtime at {dirs[0]}\n")
    log_f.flush()
    return dirs[0]


def _host_npm_install_qwen(
    prefix_dir: Path,
    npm_pkg: str,
    host_env: dict[str, str],
    install_timeout: float,
    log_f,
) -> None:
    if not shutil.which("npm"):
        raise RuntimeError(
            "Host `npm` not found; install Node.js on the machine that runs lhb "
            "so Qwen Code can be packed for the container."
        )
    prefix_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "npm",
        "install",
        "-g",
        "--omit=dev",
        "--no-fund",
        "--no-audit",
        "--prefix",
        str(prefix_dir),
        npm_pkg,
    ]
    log_f.write(f"[bootstrap] host: {' '.join(shlex.quote(c) for c in cmd)}\n")
    log_f.flush()
    proc = subprocess.run(
        cmd,
        env=host_env,
        capture_output=True,
        text=True,
        timeout=max(60.0, float(install_timeout)),
    )
    tail = 50000
    if proc.stdout:
        log_f.write("[bootstrap] host npm stdout (tail):\n")
        log_f.write(proc.stdout[-tail:] if len(proc.stdout) > tail else proc.stdout)
        log_f.write("\n")
    if proc.stderr:
        log_f.write("[bootstrap] host npm stderr (tail):\n")
        log_f.write(proc.stderr[-tail:] if len(proc.stderr) > tail else proc.stderr)
        log_f.write("\n")
    log_f.flush()
    if proc.returncode != 0:
        raise RuntimeError(
            f"Host npm install of {npm_pkg!r} failed (exit {proc.returncode}). "
            "Check registry auth and network."
        )
    qwen_sh = prefix_dir / "bin" / "qwen"
    if not (qwen_sh.is_file() or qwen_sh.is_symlink()):
        alt = prefix_dir / "node_modules" / ".bin" / "qwen"
        raise RuntimeError(
            f"Host npm install succeeded but {qwen_sh} is missing. "
            f"If you see {alt}, the install was not global-style; ensure `npm install -g --prefix …` "
            f"(benchmark uses `-g`). Unexpected layout for {npm_pkg!r}."
        )


def _tar_folder_for_put_archive(folder: Path, arcname: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(str(folder), arcname=arcname)
    buf.seek(0)
    return buf.read()


def _bundled_node_path_override(container: object, prefix: str) -> str | None:
    """Return ``PATH=…`` fragment for ``docker exec`` when ``.lhb-node`` exists."""
    p = prefix.rstrip("/")
    res = container.exec_run(["test", "-x", f"{p}/.lhb-node/bin/node"], demux=True)
    if int(res.exit_code) != 0:
        return None
    return (
        f"{p}/.lhb-node/bin:{p}/bin:"
        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    )


def _chmod_bundled_cli(container: object, prefix: str, bundled_node: bool, log_f) -> None:
    exe = f"{prefix.rstrip('/')}/bin/qwen"
    parts = [exe]
    if bundled_node:
        parts.append(f"{prefix.rstrip('/')}/.lhb-node/bin/node")
    script = "chmod a+x " + " ".join(shlex.quote(p) for p in parts)
    res = container.exec_run(["bash", "-lc", script], demux=True)
    if int(res.exit_code) != 0:
        log_f.write(f"[bootstrap] warning: chmod helper exited {res.exit_code}\n")
        log_f.flush()


def _ensure_container_qwen_cli(
    container: object, env: dict[str, str], log_f
) -> tuple[str, str | None]:
    """Return ``(qwen_executable_in_container, PATH_override_or_None)``."""
    prefix = _strip(os.environ.get("QWEN_CODE_INSTALL_PREFIX")) or "/opt/lhb-qwen-code"
    prefix = prefix.rstrip("/")
    folder_name = Path(prefix).name
    parent_on_container = str(Path(prefix).parent)

    prefixed_exe = f"{prefix}/bin/qwen"
    chk_pref = container.exec_run(["test", "-x", prefixed_exe], demux=True)
    if int(chk_pref.exit_code) == 0:
        log_f.write(f"[bootstrap] using existing bundle: {prefixed_exe}\n")
        path_ov = _bundled_node_path_override(container, prefix)
        if path_ov:
            log_f.write(
                "[bootstrap] vendored Node under prefix; will set PATH for `qwen` "
                "(outer-loop rounds reuse this).\n"
            )
        log_f.flush()
        return prefixed_exe, path_ov

    res = container.exec_run(["bash", "-lc", "command -v qwen"], demux=True)
    if int(res.exit_code) == 0:
        path = (res.output[0] or b"").decode("utf-8", errors="replace").strip()
        if path:
            log_f.write(f"[bootstrap] using existing qwen on PATH: {path}\n")
            path_ov = _bundled_node_path_override(container, prefix)
            # Only prepend vendored Node when this ``qwen`` is our packed one under ``prefix``.
            if path_ov and not (path == prefixed_exe or path.startswith(prefix + "/")):
                path_ov = None
            log_f.flush()
            return path, path_ov

    if _truthy_env("QWEN_CODE_SKIP_BOOTSTRAP"):
        raise RuntimeError(
            "No `qwen` in the client container and QWEN_CODE_SKIP_BOOTSTRAP is set."
        )

    npm_pkg = _strip(os.environ.get("QWEN_CODE_NPM_PACKAGE")) or "@qwen-code/qwen-code"
    try:
        install_timeout = float(os.environ.get("QWEN_CODE_INSTALL_TIMEOUT_SEC") or "900")
    except ValueError:
        install_timeout = 900.0
    install_timeout = max(60.0, install_timeout)

    has_node = _container_has_node(container)
    need_node = not has_node and not _falsy_env("QWEN_CODE_BUNDLE_NODE")
    if not has_node and _falsy_env("QWEN_CODE_BUNDLE_NODE"):
        raise RuntimeError(
            "The client container has no `node` on PATH and QWEN_CODE_BUNDLE_NODE=0. "
            "Install Node in the image, or allow bundling (unset QWEN_CODE_BUNDLE_NODE)."
        )

    node_ver = _strip(os.environ.get("QWEN_CODE_NODE_DIST_VERSION")) or "20.18.1"
    machine = _container_uname_m(container)
    triplet = _nodejs_dist_triplet(machine)
    slug = _bundle_slug(npm_pkg, triplet, need_node, node_ver)
    cache_root = _bundle_cache_root() / slug
    host_env = _host_npm_subprocess_env(env)

    stage_root = Path(tempfile.mkdtemp(prefix="lhb-qwen-stage-"))
    tree = stage_root / folder_name
    bundled_node = False
    try:
        cached = cache_root / folder_name
        if (cached / "bin" / "qwen").is_file() and (
            not need_node or (cached / ".lhb-node" / "bin" / "node").is_file()
        ):
            log_f.write(f"[bootstrap] reusing host cache {cached}\n")
            log_f.flush()
            shutil.copytree(cached, tree)
            bundled_node = need_node and (tree / ".lhb-node" / "bin" / "node").is_file()
        else:
            tree.mkdir(parents=True)
            _host_npm_install_qwen(tree, npm_pkg, host_env, install_timeout, log_f)
            if need_node:
                cache_root.mkdir(parents=True, exist_ok=True)
                xz = _ensure_node_tarball(triplet, node_ver, cache_root, log_f)
                extract_tmp = Path(tempfile.mkdtemp(prefix="lhb-node-extract-"))
                try:
                    node_root = _extract_node_root(xz, extract_tmp, log_f)
                    dest_node = tree / ".lhb-node"
                    if dest_node.exists():
                        shutil.rmtree(dest_node)
                    shutil.copytree(node_root, dest_node, symlinks=True)
                    bundled_node = True
                    log_f.write(f"[bootstrap] merged Node runtime into {dest_node}\n")
                    log_f.flush()
                finally:
                    shutil.rmtree(extract_tmp, ignore_errors=True)

            cache_root.mkdir(parents=True, exist_ok=True)
            if cached.exists():
                shutil.rmtree(cached, ignore_errors=True)
            shutil.copytree(tree, cached, symlinks=True)

        tar_bytes = _tar_folder_for_put_archive(tree, folder_name)
        log_f.write(
            f"[bootstrap] uploading bundle → {parent_on_container}/"
            f"{folder_name} ({len(tar_bytes)} bytes)\n"
        )
        log_f.flush()
        container.put_archive(parent_on_container, tar_bytes)
        _chmod_bundled_cli(container, prefix, bundled_node, log_f)

    finally:
        shutil.rmtree(stage_root, ignore_errors=True)

    exe = f"{prefix}/bin/qwen"
    chk = container.exec_run(["test", "-x", exe], demux=True)
    if int(chk.exit_code) != 0:
        raise RuntimeError(f"After put_archive, {exe} is not executable in the container.")

    path_override = _bundled_node_path_override(container, prefix)

    log_f.write(
        f"[bootstrap] ready: {exe}"
        + (" (PATH includes bundled node)\n" if path_override else "\n")
    )
    log_f.flush()
    return exe, path_override


def _resolve_client_workspace(container: object) -> str:
    res = container.exec_run(["test", "-d", "/workspace"], demux=True)
    if int(res.exit_code) == 0:
        return "/workspace"
    wd = container.attrs.get("Config", {}).get("WorkingDir") or ""
    if isinstance(wd, str) and wd.startswith("/"):
        return wd
    return "/workspace"


def _put_instruction_file_in_container(container: object, content: str) -> None:
    payload = content.encode("utf-8")
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w") as tar:
        info = tarfile.TarInfo(name=Path(_INSTRUCTION_CONTAINER_PATH).name)
        info.size = len(payload)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(payload))
    tar_buffer.seek(0)
    container.put_archive("/tmp", tar_buffer.getvalue())


def _container_exec_env_list(env: dict[str, str]) -> list[str]:
    """Keys forwarded into ``docker exec`` (avoid dumping the whole host env)."""
    keys = (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "DASHSCOPE_API_KEY",
        "BAILIAN_API_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
    )
    out: list[str] = []
    for k in keys:
        v = env.get(k)
        if v is not None and str(v) != "":
            out.append(f"{k}={str(v)}")
    return out


def _global_npm_cli_js(prefix: Path, npm_pkg: str) -> Path:
    """``npm install -g --prefix P`` layout: ``P/lib/node_modules/<scoped?>/cli.js``."""
    parts = [x for x in npm_pkg.split("/") if x]
    if len(parts) >= 2 and parts[0].startswith("@"):
        return prefix / "lib" / "node_modules" / parts[0] / parts[1] / "cli.js"
    if len(parts) == 1:
        return prefix / "lib" / "node_modules" / parts[0] / "cli.js"
    return prefix / "lib" / "node_modules" / "/".join(parts) / "cli.js"


def _bash_exec_line_for_qwen(qwen_argv: list[str]) -> str:
    """Build ``bash -lc`` script: run ``cli.js`` with ``node``, not the ``bin/qwen`` symlink.

    Invoking the ``bin/qwen`` symlink as the main script can make Node treat the
    file as CommonJS and reject ESM ``import`` even when ``package.json`` has
    ``"type": "module"``. Prefer the real ``lib/node_modules/.../cli.js`` path from
    the global npm layout, then fall back to ``readlink -f`` / the launcher path.
    """
    npm_pkg = _strip(os.environ.get("QWEN_CODE_NPM_PACKAGE")) or "@qwen-code/qwen-code"
    exe = str(qwen_argv[0])
    rest = [str(x) for x in qwen_argv[1:]]
    instr = shlex.quote(_INSTRUCTION_CONTAINER_PATH)
    rest_sh = shlex.join(rest) if rest else ""
    space_rest = f" {rest_sh}" if rest_sh else ""
    p = Path(exe)
    if p.is_absolute() and p.parent.name == "bin":
        prefix = p.parent.parent
        cli_js = _global_npm_cli_js(prefix, npm_pkg)
        bundled_node = prefix / ".lhb-node" / "bin" / "node"
        eq, bq, cq = (
            shlex.quote(exe),
            shlex.quote(str(bundled_node)),
            shlex.quote(cli_js.as_posix()),
        )
        return (
            f"if [ -f {cq} ]; then CLI={cq}; "
            f'else CLI=$(readlink -f {eq} 2>/dev/null || true); '
            f'test -z "$CLI" && CLI={eq}; fi; '
            f"if [ -x {bq} ]; then exec {bq} \"$CLI\"{space_rest} < {instr}; "
            f'elif command -v node >/dev/null 2>&1; then exec node "$CLI"{space_rest} < {instr}; '
            f"else exec {eq}{space_rest} < {instr}; fi"
        )
    return f"{shlex.join([exe, *rest])} < {instr}"


def _run_qwen_in_container(
    container: object,
    workdir: str,
    env: dict[str, str],
    qwen_argv: list[str],
    traj_f,
    log_f,
    wall_timeout: float,
    path_env: str | None = None,
) -> tuple[bool, int | None]:
    """Run ``qwen`` inside the client container; stream stdout/stderr to files.

    Returns ``(timed_out, exit_code)``. ``exit_code`` is ``None`` if we timed out
    before ``exec_inspect`` could be read reliably.
    """
    inner = _bash_exec_line_for_qwen(qwen_argv)
    api = container.client.api
    exec_env = _container_exec_env_list(env)
    if path_env:
        exec_env.append(f"PATH={path_env}")
    exec_id = api.exec_create(
        container.id,
        ["bash", "-lc", inner],
        stdout=True,
        stderr=True,
        stdin=False,
        tty=False,
        environment=exec_env or None,
        workdir=workdir,
    )["Id"]
    stream = api.exec_start(exec_id, demux=True, stream=True)
    deadline = time.monotonic() + float(wall_timeout)
    timed_out = False
    try:
        for chunk in stream:
            if time.monotonic() > deadline:
                timed_out = True
                break
            if not chunk:
                continue
            out_b, err_b = chunk
            if out_b:
                traj_f.write(out_b.decode("utf-8", errors="replace"))
            if err_b:
                log_f.write(err_b.decode("utf-8", errors="replace"))
            traj_f.flush()
            log_f.flush()
    finally:
        try:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        except Exception:
            pass

    if timed_out:
        try:
            log_f.write(
                "\n[qwen container exec exceeded "
                f"timeout_sec={wall_timeout!r}]\n"
            )
        except OSError:
            pass
        return True, None

    exit_code = int(api.exec_inspect(exec_id).get("ExitCode", -1))
    if exit_code != 0:
        try:
            log_f.write(f"\n[exit code {exit_code}]\n")
        except OSError:
            pass
    return False, exit_code


class QwenCodeAgent(BaseAgent):
    """Run Qwen Code in the client container (default) or host mirror (opt-in)."""

    def __init__(self, model_name: str, **kwargs):
        super().__init__(**kwargs)
        self._model_name = model_name
        self._model = self._normalize_model_id(model_name)
        self._validate_host_credentials()

    @staticmethod
    def _normalize_model_id(model_name: str) -> str:
        """Map harness ``-m`` values to Qwen ``-m`` model ids."""
        if model_name in ("", "Oracle"):
            raise ValueError(
                "QwenCodeAgent requires a real model id (e.g. `qwen3-coder-plus`). "
                "Pass `-m` / `--model` when using `--agent qwen-code`."
            )
        if "/" in model_name:
            provider, bare = model_name.split("/", 1)
            if provider in ("qwen-code", "bailian", "dashscope"):
                return bare
            # e.g. openai/qwen3-coder-plus — use the right-hand side for Qwen CLI
            return bare
        return model_name

    @staticmethod
    def name() -> str:
        return AgentName.QWEN_CODE.value

    def _validate_host_credentials(self) -> None:
        """Fail fast if the host shell cannot supply an API key."""
        probe = {k: str(v) for k, v in os.environ.items()}
        if not _openai_api_key_from_env(probe):
            raise ValueError(
                "QwenCodeAgent: set DASHSCOPE_API_KEY, BAILIAN_API_KEY, or OPENAI_API_KEY "
                "in the environment of the process that runs lhb (the same rules as your "
                "working curl test)."
            )

    def get_trajectory_paths(self) -> list[str]:
        # Trajectory is written on the host under logging_dir; no container copy.
        return []

    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
        timeout_sec: float | None = None,
    ) -> AgentResult:
        container = session._container
        wall_timeout = timeout_sec if timeout_sec is not None else 86400.0
        mode = _workspace_mode()
        use_container = mode == "container"

        if logging_dir is not None:
            logging_dir.mkdir(parents=True, exist_ok=True)
            traj_path = logging_dir / _TRAJECTORY_REL
            traj_path.parent.mkdir(parents=True, exist_ok=True)
            log_path = logging_dir / "qwen_code.log"
            (logging_dir / "lhb_instruction.txt").write_text(
                instruction, encoding="utf-8"
            )
        else:
            traj_path = Path(_TRAJECTORY_REL)
            traj_path.parent.mkdir(parents=True, exist_ok=True)
            log_path = Path("qwen_code.log")

        env = os.environ.copy()
        auth_flags, _resolved_key = _prepare_qwen_child_env_and_cli_flags(env)
        timed_out = False

        if use_container:
            workdir = _resolve_client_workspace(container)
            with open(traj_path, "w", encoding="utf-8") as traj_f, open(
                log_path, "w", encoding="utf-8"
            ) as log_f:
                qwen_exe, path_env = _ensure_container_qwen_cli(container, env, log_f)
                qwen_argv: list[str] = [
                    qwen_exe,
                    *auth_flags,
                    "-y",
                    "-m",
                    self._model,
                    "-o",
                    "stream-json",
                ]
                _put_instruction_file_in_container(container, instruction)
                timed_out, _exit = _run_qwen_in_container(
                    container,
                    workdir,
                    env,
                    qwen_argv,
                    traj_f,
                    log_f,
                    wall_timeout,
                    path_env,
                )
        else:
            mirror_workdir = (
                container.attrs.get("Config", {}).get("WorkingDir") or "/workspace"
            )
            with tempfile.TemporaryDirectory(prefix="lhb-qwen-code-") as tmp_str:
                tmp = Path(tmp_str)
                bits, _ = container.get_archive(mirror_workdir)
                with tarfile.open(fileobj=io.BytesIO(b"".join(bits))) as tar:
                    tar.extractall(tmp)
                local_ws = tmp / Path(mirror_workdir).name

                if logging_dir is None:
                    traj_path = local_ws / _TRAJECTORY_REL
                    traj_path.parent.mkdir(parents=True, exist_ok=True)
                    log_path = local_ws / "qwen_code.log"

                env["HOME"] = str(Path.home())
                cmd: list[str | Path] = [
                    str(_resolve_qwen_executable()),
                    *auth_flags,
                    "-y",
                    "-m",
                    self._model,
                    "-o",
                    "stream-json",
                ]
                (tmp / "lhb_instruction.txt").write_text(instruction, encoding="utf-8")
                with open(traj_path, "w", encoding="utf-8") as traj_f, open(
                    log_path, "w", encoding="utf-8"
                ) as log_f:
                    try:
                        proc = subprocess.run(
                            [str(c) for c in cmd],
                            cwd=str(local_ws),
                            env=env,
                            timeout=wall_timeout,
                            stdout=traj_f,
                            stderr=log_f,
                            text=True,
                            input=instruction,
                        )
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        try:
                            log_f.write(
                                "\n[qwen subprocess exceeded "
                                f"timeout_sec={wall_timeout!r}]\n"
                            )
                        except OSError:
                            pass
                    else:
                        if proc.returncode != 0:
                            log_f.write(f"\n[exit code {proc.returncode}]\n")

                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w") as tar:
                    tar.add(local_ws, arcname=".")
                buf.seek(0)
                container.put_archive(mirror_workdir, buf.read())

        if timed_out:
            return AgentResult(failure_mode=FailureMode.AGENT_TIMEOUT.value)
        return AgentResult()
