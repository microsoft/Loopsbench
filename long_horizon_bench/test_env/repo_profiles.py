"""
Minimal repo/test metadata for scripts/validate_per_pr.py (host run).

Kept small on purpose: extend when new repo_id / language paths need first-class support.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path

import yaml


def infer_repo_id(task_dir: Path, fallback_repo_id: str | None = None) -> str:
    dag = task_dir / "unit_dag.json"
    if dag.is_file():
        try:
            data = json.loads(dag.read_text())
        except Exception:
            data = None
        if isinstance(data, dict):
            rid = data.get("repo_id")
            if isinstance(rid, str) and rid.strip():
                return rid.strip()
    name = task_dir.name
    if name.startswith("task_"):
        return name.removeprefix("task_")
    return (fallback_repo_id or name).strip() or name


def infer_language(task_dir: Path, fallback_repo_id: str | None = None) -> str:
    ty = task_dir / "task.yaml"
    if ty.is_file():
        try:
            data = yaml.safe_load(ty.read_text()) or {}
        except Exception:
            data = {}
        # Parser reflects how tests are executed (e.g. pytest → Python harness); it should
        # win over incidental tags like `cpp` on C++ codebases that still use pytest drivers.
        parser = str(data.get("parser_name") or "").lower()
        if parser in {"pytest", "pytest_json"}:
            return "python"
        tags = data.get("tags") or []
        if isinstance(tags, list):
            for t in tags:
                if isinstance(t, str) and t.lower() in {"python", "java", "go", "js", "cpp", "rust"}:
                    return t.lower()
    rid = (fallback_repo_id or infer_repo_id(task_dir)).lower()
    if rid in {"django", "ansible", "tensorflow", "node"}:
        return "python"
    return "python"


def infer_repo_root(task_dir: Path) -> str:
    ty = task_dir / "task.yaml"
    if ty.is_file():
        try:
            text = ty.read_text()
        except Exception:
            text = ""
        # Dockerfile COPY base/ → /workspace/repo/; gold patches use paths like a/src/... and
        # validate_per_pr stages tests under repo_root — must match /workspace/repo, not /workspace.
        if "/workspace/repo/" in text:
            return "/workspace/repo"
        for line in text.splitlines():
            if "/workspace" in line and "working directory" in line.lower():
                return "/workspace"
    return "/workspace"


def select_test_entry_files(repo_id: str, files: list[str]) -> bool:
    if not files:
        return False
    if repo_id == "rails":
        return any(str(f).endswith(("_test.rb", "_spec.rb")) for f in files)
    suffixes = (".py", ".go", ".java", ".js", ".rs", ".cpp", ".cc", ".c", ".ts", ".php")
    return any(str(f).endswith(suffixes) for f in files)


def _pytest_selectable_entries(test_files: list[str]) -> list[str]:
    """Paths for `python -m pytest`: *.py files or *.py::nodeid selections."""
    out: list[str] = []
    for f in test_files:
        if f.endswith(".py"):
            out.append(f)
            continue
        if "::" in f:
            head, _, _rest = f.partition("::")
            if head.endswith(".py"):
                out.append(f)
    return out


def _pytest_abs_args(repo_root: str, entries: list[str]) -> str:
    parts: list[str] = []
    for f in entries:
        if "::" in f:
            path, _, sel = f.partition("::")
            parts.append(shlex.quote(f"{repo_root}/{path}::{sel}"))
        else:
            parts.append(shlex.quote(f"{repo_root}/{f}"))
    return " ".join(parts)


def build_native_test_cmd(*, repo_id: str, test_files: list[str], repo_root: str) -> list[str]:
    rr = shlex.quote(repo_root)
    py_files = _pytest_selectable_entries(test_files)
    if not py_files:
        return ["bash", "-c", "echo 'no python test files'; exit 0"]

    if repo_id == "django":
        modules: list[str] = []
        for f in py_files:
            if f.startswith("tests/"):
                mod = f[len("tests/") :].replace("/", ".").removesuffix(".py")
                if mod:
                    modules.append(mod)
        if not modules:
            return ["bash", "-c", "echo 'no django test modules'; exit 0"]
        joined = " ".join(shlex.quote(m) for m in modules)
        return [
            "bash",
            "-c",
            f"set -o pipefail; cd {rr}/tests && RUNNING_DJANGOS_TEST_SUITE=true PYTHONPATH={rr} "
            f"python3 runtests.py --settings=test_sqlite --parallel=1 {joined} 2>&1 | tail -50",
        ]

    if repo_id == "ansible":
        abs_files = " ".join(shlex.quote(f"{repo_root}/{f}") for f in py_files)
        return [
            "bash",
            "-c",
            f"set -o pipefail; cd {rr} && PYTHONPATH={rr}/lib python3 -m pytest -x --tb=short -q {abs_files} 2>&1 | tail -50",
        ]

    if repo_id == "berkeleycs61b":
        abs_files = " ".join(shlex.quote(f"{repo_root}/{f}") for f in py_files)
        py = "/opt/lhb-testing/bin/python"
        return [
            "bash",
            "-c",
            f"set -o pipefail; {py} -m pytest -x --tb=short -q {abs_files} 2>&1 | tail -50",
        ]

    if repo_id == "cmu15-462":
        abs_files = " ".join(shlex.quote(f"{repo_root}/{f}") for f in py_files)
        return [
            "bash",
            "-c",
            f"set -o pipefail; cd {rr} && node Maekfile.js -j4 >/tmp/lhb_maek.log 2>&1; maek_rc=$?; tail -40 /tmp/lhb_maek.log; "
            f'[ "$maek_rc" -eq 0 ] || exit "$maek_rc"; '
            f"python3 -m pytest -x --tb=short -q {abs_files} 2>&1 | tail -80",
        ]

    # CS285: each homework reuses package names (infrastructure, agents, …).
    # Dockerfile may set a combined PYTHONPATH; per-PR pytest must override with one tree.
    if repo_id == "cs285":
        py_pick = 'PY=python3; [ -x /opt/test-venv/bin/python ] && PY=/opt/test-venv/bin/python'

        def _pp_for(rel: str) -> str:
            base = Path(rel).name
            table = {
                "test_hw1.py": f"{repo_root}/hw1/src",
                "test_outputs.py": f"{repo_root}/hw2/src",
                "test_hw3.py": f"{repo_root}/hw3/src",
                "test_hw4.py": f"{repo_root}/hw4",
                "test_hw5.py": f"{repo_root}/hw5/src",
            }
            return table.get(base, f"{repo_root}/hw3/src")

        steps: list[str] = []
        for rel in sorted(py_files):
            pp = shlex.quote(_pp_for(rel))
            abs_one = shlex.quote(f"{repo_root}/{rel}")
            steps.append(
                f"export PYTHONPATH={pp}; {py_pick}; "
                f'"$PY" -m pytest -x --tb=short -q {abs_one}'
            )
        body = "set -eo pipefail; " + " && ".join(steps)
        return ["bash", "-c", f"set -o pipefail; {body} 2>&1 | tail -80"]

    # fdcompiler: validate_per_pr resets with `git clean -fdX`, which removes ignored `build/`.
    # Per-PR pytest expects `build/bin/LLVMCompiler`; rebuild before each pytest run.
    if repo_id == "fdcompiler":
        abs_files = _pytest_abs_args(repo_root, py_files)
        venv_py = "/opt/test-venv/bin/python"
        return [
            "bash",
            "-c",
            "set -o pipefail; "
            f"cd {rr} && make build >/tmp/lhb_fdcompiler_make.log 2>&1 || {{ tail -80 /tmp/lhb_fdcompiler_make.log; exit 1; }}; "
            'PY=python3; '
            f'if [ -x "{venv_py}" ] && "{venv_py}" -m pytest --version >/dev/null 2>&1; then '
            f'PY="{venv_py}"; fi; '
            f'$PY -m pytest -x --tb=short -q {abs_files} 2>&1 | tail -50',
        ]

    # pkucompiler (Rust): per-PR workspace state needs a fresh release build for tests.
    if repo_id == "pkucompiler":
        abs_files = _pytest_abs_args(repo_root, py_files)
        venv_py = "/opt/test-venv/bin/python"
        return [
            "bash",
            "-c",
            "set -o pipefail; "
            "export PATH=/usr/local/cargo/bin:/usr/local/rustup/bin:$PATH; "
            f"cd {rr} && cargo build --release >/tmp/lhb_pkucompiler_cargo.log 2>&1 || "
            f"{{ tail -80 /tmp/lhb_pkucompiler_cargo.log; exit 1; }}; "
            'PY=python3; '
            f'if [ -x "{venv_py}" ] && "{venv_py}" -m pytest --version >/dev/null 2>&1; then '
            f'PY="{venv_py}"; fi; '
            f'$PY -m pytest -x --tb=short -q {abs_files} 2>&1 | tail -50',
        ]

    # shustru: validate_per_pr resets with `git clean -fdX`, which removes ignored `build/`.
    # Tests shell out to binaries under /workspace/build/...; rebuild before pytest.
    if repo_id == "shustru":
        abs_files = _pytest_abs_args(repo_root, py_files)
        venv_py = "/opt/test-venv/bin/python"
        return [
            "bash",
            "-c",
            "set -o pipefail; "
            f"cd {rr} && mkdir -p build && cd build && "
            f"cmake .. -DCMAKE_BUILD_TYPE=Release >/tmp/lhb_shustru_cmake.log 2>&1 && "
            f"cmake --build . -j8 >/tmp/lhb_shustru_build.log 2>&1 || "
            f"{{ tail -80 /tmp/lhb_shustru_cmake.log /tmp/lhb_shustru_build.log 2>/dev/null; exit 1; }}; "
            'PY=python3; '
            f'if [ -x "{venv_py}" ] && "{venv_py}" -m pytest --version >/dev/null 2>&1; then '
            f'PY="{venv_py}"; fi; '
            f'$PY -m pytest -x --tb=short -q {abs_files} 2>&1 | tail -50',
        ]

    # ucsdcompiler (Rust): `git clean -fdX` drops `target/`; pre-pytest `cargo build` matches
    # the per-suite autouse fixture and avoids races when the harness runs pytest directly.
    if repo_id == "ucsdcompiler":
        abs_files = _pytest_abs_args(repo_root, py_files)
        venv_py = "/opt/test-venv/bin/python"
        return [
            "bash",
            "-c",
            "set -o pipefail; "
            "export PATH=/usr/local/cargo/bin:/usr/local/rustup/bin:$PATH; "
            f"cd {rr}/assignment8-GreenSnake && cargo build >/tmp/lhb_ucsdcompiler_cargo.log 2>&1 || "
            f"{{ tail -80 /tmp/lhb_ucsdcompiler_cargo.log; exit 1; }}; "
            'PY=python3; '
            f'if [ -x "{venv_py}" ] && "{venv_py}" -m pytest --version >/dev/null 2>&1; then '
            f'PY="{venv_py}"; fi; '
            f'$PY -m pytest -x --tb=short -q {abs_files} 2>&1 | tail -50',
        ]

    # ucascn: pytest drives a native server binary; build httpserver before pytest (validate_per_pr
    # per-PR path does not run run-tests.sh).
    if repo_id == "ucascn":
        abs_files = _pytest_abs_args(repo_root, py_files)
        venv_py = "/opt/test-venv/bin/python"
        return [
            "bash",
            "-c",
            "set -o pipefail; "
            f"mkdir -p {rr}/计算机网络实验/httpserver/build && "
            f"cd {rr}/计算机网络实验/httpserver/build && cmake .. -DCMAKE_BUILD_TYPE=Release "
            f">/tmp/lhb_ucascn_cmake.log 2>&1 && make >/tmp/lhb_ucascn_make.log 2>&1 || "
            f"{{ tail -40 /tmp/lhb_ucascn_cmake.log /tmp/lhb_ucascn_make.log 2>/dev/null; exit 1; }}; "
            'PY=python3; '
            f'if [ -x "{venv_py}" ] && "{venv_py}" -m pytest --version >/dev/null 2>&1; then '
            f'PY="{venv_py}"; fi; '
            f'$PY -m pytest -x --tb=short -q {abs_files} 2>&1 | tail -80',
        ]

    # hgddatabase: pytest shells out to QueryProcessing/QueryOptimize; per-PR patches change C++
    # so we rebuild. If ``git clean`` removed build trees (untracked), configure then build.
    if repo_id == "hgddatabase":
        abs_files = _pytest_abs_args(repo_root, py_files)
        venv_py = "/opt/test-venv/bin/python"
        return [
            "bash",
            "-c",
            "set -o pipefail; "
            f"root={rr}; "
            f'QP="$root/lab/QueryProcessing/build"; QO="$root/lab/QueryOptimize/build"; '
            f'if [ ! -x "$QP/QueryProcessing" ]; then '
            f'cmake -S "$root/lab/QueryProcessing" -B "$QP" >/tmp/lhb_hgd_qp_cmake.log 2>&1 || '
            f'{{ tail -80 /tmp/lhb_hgd_qp_cmake.log; exit 1; }}; fi; '
            f'cmake --build "$QP" >/tmp/lhb_hgd_qp.log 2>&1 || {{ tail -80 /tmp/lhb_hgd_qp.log; exit 1; }}; '
            f'if [ ! -x "$QO/QueryOptimize" ]; then '
            f'cmake -S "$root/lab/QueryOptimize" -B "$QO" >/tmp/lhb_hgd_qo_cmake.log 2>&1 || '
            f'{{ tail -80 /tmp/lhb_hgd_qo_cmake.log; exit 1; }}; fi; '
            f'cmake --build "$QO" >/tmp/lhb_hgd_qo.log 2>&1 || {{ tail -80 /tmp/lhb_hgd_qo.log; exit 1; }}; '
            'PY=python3; '
            f'if [ -x "{venv_py}" ] && "{venv_py}" -m pytest --version >/dev/null 2>&1; then '
            f'PY="{venv_py}"; fi; '
            f'$PY -m pytest -x --tb=short -q {abs_files} 2>&1 | tail -50',
        ]

    # cocos2dx_physics2d_medium: same pattern as navmesh — materialize /workspace/output before pytest.
    if repo_id == "cocos2dx_physics2d_medium":
        abs_files = _pytest_abs_args(repo_root, py_files)
        venv_py = "/opt/test-venv/bin/python"
        return [
            "bash",
            "-c",
            "set -o pipefail; "
            "bash /workspace/scripts/lhb_materialize_physics2d_outputs.sh "
            ">/tmp/lhb_physics2d_materialize.log 2>&1 || "
            "{ tail -120 /tmp/lhb_physics2d_materialize.log; exit 1; }; "
            'PY=python3; '
            f'if [ -x "{venv_py}" ] && "{venv_py}" -m pytest --version >/dev/null 2>&1; then '
            f'PY="{venv_py}"; fi; '
            f'$PY -m pytest -x --tb=short -q {abs_files} 2>&1 | tail -80',
        ]

    # monogame_math2d_hard: per-PR pytest reads /workspace/output from dotnet build + headless replays.
    if repo_id == "monogame_math2d_hard":
        abs_files = _pytest_abs_args(repo_root, py_files)
        venv_py = "/opt/test-venv/bin/python"
        return [
            "bash",
            "-c",
            "set -o pipefail; "
            "bash /workspace/scripts/lhb_materialize_monogame_math_outputs.sh "
            ">/tmp/lhb_monogame_materialize.log 2>&1 || "
            "{ tail -120 /tmp/lhb_monogame_materialize.log; exit 1; }; "
            'PY=python3; '
            f'if [ -x "{venv_py}" ] && "{venv_py}" -m pytest --version >/dev/null 2>&1; then '
            f'PY="{venv_py}"; fi; '
            f'$PY -m pytest -x --tb=short -q {abs_files} 2>&1 | tail -80',
        ]

    # monogame_transform3d_hard: same pattern as monogame_math2d — materialize before per-PR pytest.
    if repo_id == "monogame_transform3d_hard":
        abs_files = _pytest_abs_args(repo_root, py_files)
        venv_py = "/opt/test-venv/bin/python"
        return [
            "bash",
            "-c",
            "set -o pipefail; "
            "bash /workspace/scripts/lhb_materialize_transform3d_outputs.sh "
            ">/tmp/lhb_monogame_transform3d_materialize.log 2>&1 || "
            "{ tail -120 /tmp/lhb_monogame_transform3d_materialize.log; exit 1; }; "
            'PY=python3; '
            f'if [ -x "{venv_py}" ] && "{venv_py}" -m pytest --version >/dev/null 2>&1; then '
            f'PY="{venv_py}"; fi; '
            f'$PY -m pytest -x --tb=short -q {abs_files} 2>&1 | tail -80',
        ]

    # cocos2dx_navmesh_hard: per-PR pytest reads /workspace/output artifacts produced only by
    # build + engine checks + headless replays. validate_per_pr does not invoke run-tests.sh for
    # these runs; strict *after* replays the full solution order (see _strict_after_patch_chain).
    if repo_id == "cocos2dx_navmesh_hard":
        abs_files = _pytest_abs_args(repo_root, py_files)
        venv_py = "/opt/test-venv/bin/python"
        return [
            "bash",
            "-c",
            "set -o pipefail; "
            "bash /workspace/scripts/lhb_materialize_navmesh_outputs.sh "
            ">/tmp/lhb_navmesh_materialize.log 2>&1 || "
            "{ tail -120 /tmp/lhb_navmesh_materialize.log; exit 1; }; "
            'PY=python3; '
            f'if [ -x "{venv_py}" ] && "{venv_py}" -m pytest --version >/dev/null 2>&1; then '
            f'PY="{venv_py}"; fi; '
            f'$PY -m pytest -x --tb=short -q {abs_files} 2>&1 | tail -80',
        ]

    # godot_bootstrap_hard: per-PR pytest reads /workspace/output artifacts produced only by
    # Godot build + engine doctest run + headless replays. validate_per_pr does not invoke run-tests.sh.
    if repo_id == "godot_bootstrap_hard":
        abs_files = _pytest_abs_args(repo_root, py_files)
        venv_py = "/opt/test-venv/bin/python"
        return [
            "bash",
            "-c",
            "set -o pipefail; "
            "bash /workspace/scripts/lhb_materialize_godot_bootstrap_outputs.sh "
            ">/tmp/lhb_godot_materialize.log 2>&1 || "
            "{ tail -120 /tmp/lhb_godot_materialize.log; exit 1; }; "
            'PY=python3; '
            f'if [ -x "{venv_py}" ] && "{venv_py}" -m pytest --version >/dev/null 2>&1; then '
            f'PY="{venv_py}"; fi; '
            f'$PY -m pytest -x --tb=short -q {abs_files} 2>&1 | tail -80',
        ]

    # godot_navigation2d_hard: same pattern as cocos2dx_navmesh — materialize /workspace/output before pytest.
    if repo_id == "godot_navigation2d_hard":
        abs_files = _pytest_abs_args(repo_root, py_files)
        venv_py = "/opt/test-venv/bin/python"
        return [
            "bash",
            "-c",
            "set -o pipefail; "
            "bash /workspace/scripts/lhb_materialize_navigation2d_outputs.sh "
            ">/tmp/lhb_nav2d_materialize.log 2>&1 || "
            "{ tail -120 /tmp/lhb_nav2d_materialize.log; exit 1; }; "
            'PY=python3; '
            f'if [ -x "{venv_py}" ] && "{venv_py}" -m pytest --version >/dev/null 2>&1; then '
            f'PY="{venv_py}"; fi; '
            f'$PY -m pytest -x --tb=short -q {abs_files} 2>&1 | tail -80',
        ]

    # panda3d_collision_hard: pytest reads /workspace/output from Panda build + engine checks + replays.
    if repo_id == "panda3d_collision_hard":
        abs_files = _pytest_abs_args(repo_root, py_files)
        venv_py = "/opt/test-venv/bin/python"
        return [
            "bash",
            "-c",
            "set -o pipefail; "
            "bash /workspace/scripts/lhb_materialize_panda_collision_outputs.sh "
            ">/tmp/lhb_panda_collision_materialize.log 2>&1 || "
            "{ tail -120 /tmp/lhb_panda_collision_materialize.log; exit 1; }; "
            'PY=python3; '
            f'if [ -x "{venv_py}" ] && "{venv_py}" -m pytest --version >/dev/null 2>&1; then '
            f'PY="{venv_py}"; fi; '
            f'$PY -m pytest -x --tb=short -q {abs_files} 2>&1 | tail -80',
        ]

    # panda3d_pgraph_hard: pytest reads /workspace/output from Panda build + engine checks + replays.
    if repo_id == "panda3d_pgraph_hard":
        abs_files = _pytest_abs_args(repo_root, py_files)
        venv_py = "/opt/test-venv/bin/python"
        return [
            "bash",
            "-c",
            "set -o pipefail; "
            "bash /workspace/scripts/lhb_materialize_pgraph_outputs.sh "
            ">/tmp/lhb_pgraph_materialize.log 2>&1 || "
            "{ tail -120 /tmp/lhb_pgraph_materialize.log; exit 1; }; "
            'PY=python3; '
            f'if [ -x "{venv_py}" ] && "{venv_py}" -m pytest --version >/dev/null 2>&1; then '
            f'PY="{venv_py}"; fi; '
            f'$PY -m pytest -x --tb=short -q {abs_files} 2>&1 | tail -80',
        ]

    # raylib_textures_hard: pytest asserts /workspace/output from raylib build + replays.
    if repo_id == "raylib_textures_hard":
        abs_files = _pytest_abs_args(repo_root, py_files)
        venv_py = "/opt/test-venv/bin/python"
        return [
            "bash",
            "-c",
            "set -o pipefail; "
            "bash /workspace/scripts/lhb_materialize_texture_outputs.sh "
            ">/tmp/lhb_raylib_textures_materialize.log 2>&1 || "
            "{ tail -120 /tmp/lhb_raylib_textures_materialize.log; exit 1; }; "
            'PY=python3; '
            f'if [ -x "{venv_py}" ] && "{venv_py}" -m pytest --version >/dev/null 2>&1; then '
            f'PY="{venv_py}"; fi; '
            f'$PY -m pytest -x --tb=short -q {abs_files} 2>&1 | tail -80',
        ]

    abs_files = _pytest_abs_args(repo_root, py_files)
    # Prefer /opt/test-venv only when it can run pytest; some images ship a stub venv or
    # install pytest only on system python (see e.g. task_mit6-1600).
    venv_py = "/opt/test-venv/bin/python"
    return [
        "bash",
        "-c",
        "set -o pipefail; PY=python3; "
        f'if [ -x "{venv_py}" ] && "{venv_py}" -m pytest --version >/dev/null 2>&1; then '
        f'PY="{venv_py}"; fi; '
        f'$PY -m pytest -x --tb=short -q {abs_files} 2>&1 | tail -50',
    ]


def classify_test_failure(
    stdout: str,
    stderr: str,
    command: object | None,
) -> tuple[str | None, str | None]:
    text = f"{stdout or ''}\n{stderr or ''}"
    low = text.lower()
    if "timeout" in low or "timed out" in low:
        return "timeout", text.strip()[-800:] or None
    if "error" in low or "fail" in low:
        return "test_failure", text.strip()[-800:] or None
    if text.strip():
        return "unknown", text.strip()[-800:]
    return None, None
