"""Snapshot agent workspace (container A) into tester workspace (container B) via shared volume.

The shared volume `lhb_shared` is mounted at two different paths on purpose:

  - Agent (client) container:  /var/lib/lhb/.mirror   (hidden from the agent prompt)
  - Tester container:          /shared                (classic path, for pytest runs)

We avoid rsync'ing A's /workspace directly because A may be writing concurrently.
`docker exec A rsync /workspace/ -> <hidden>/snapshots/<ts>/` produces a stable
copy; tester then reads the same data at /shared/snapshots/<ts>/.

Layout inside the shared volume:
  snapshots/<ts>/  — per-snapshot workspace copy (tester-owned semantics)
  (feedback/       — no longer used; regression_status.json is host-side only.)
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from long_horizon_bench.terminal.docker_compose_manager import docker_command, docker_env


@dataclass
class SnapshotResult:
    snapshot_id: str
    path: str  # path inside the TESTER container (always /shared/…)
    exit_code: int
    stderr: str


class Snapshotter:
    def __init__(
        self,
        agent_container: str,
        workspace_dir: str = "/workspace",
        # Where the shared volume is mounted inside the AGENT container.
        # This is intentionally different from the tester side (/shared) to
        # keep the regression plumbing out of the agent's view.
        shared_root_agent: str = "/var/lib/lhb/.mirror",
        # Where the same shared volume is mounted inside the TESTER container.
        shared_root_tester: str = "/shared",
    ) -> None:
        self.agent_container = agent_container
        self.workspace_dir = workspace_dir.rstrip("/")
        self.shared_root_agent = shared_root_agent.rstrip("/")
        self.shared_root_tester = shared_root_tester.rstrip("/")

    def ensure_dirs(self) -> None:
        subprocess.run(
            [
                *docker_command(["exec", self.agent_container, "sh", "-c"]),
                f"mkdir -p {self.shared_root_agent}/snapshots && "
                f"(command -v rsync >/dev/null || (apt-get update -qq && apt-get install -y -qq rsync >/dev/null))",
            ],
            check=False, capture_output=True, env=docker_env(), timeout=120,
        )

    def take(self) -> SnapshotResult:
        ts = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time()*1000)%1000:03d}"
        agent_dest = f"{self.shared_root_agent}/snapshots/{ts}"
        tester_path = f"{self.shared_root_tester}/snapshots/{ts}"
        cmd = [
            *docker_command(["exec", self.agent_container, "sh", "-c"]),
            (
                f"mkdir -p {agent_dest} && "
                f"rsync -a --delete "
                f"--exclude='.git' --exclude='node_modules' --exclude='__pycache__' "
                f"--exclude='.harness' "
                f"{self.workspace_dir}/ {agent_dest}/"
            ),
        ]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, env=docker_env(), timeout=600,
            )
        except subprocess.TimeoutExpired as e:
            return SnapshotResult(
                snapshot_id=ts, path=tester_path, exit_code=124,
                stderr=f"snapshot timeout: {e}",
            )
        return SnapshotResult(
            snapshot_id=ts, path=tester_path, exit_code=r.returncode, stderr=r.stderr,
        )

    def prune(self, keep: int = 3, keep_ids: list[str] | None = None) -> None:
        keep_ids = keep_ids or []
        # Build a whitelist guard so newly-created snapshots in flight aren't
        # racy-deleted between `ls` and `rm`.
        guard = ""
        if keep_ids:
            quoted = " ".join(f"'{kid}'" for kid in keep_ids)
            guard = f"keep=$(printf '%s\\n' {quoted}); "
            filter_expr = (
                "grep -v -F -x -f <(printf '%s\\n' \"$keep\") || true"
            )
        else:
            filter_expr = "cat"
        subprocess.run(
            [
                *docker_command(["exec", self.agent_container, "bash", "-c"]),
                f"cd {self.shared_root_agent}/snapshots 2>/dev/null && "
                f"{guard}"
                f"ls -1t | tail -n +{keep+1} | {filter_expr} | xargs -r rm -rf",
            ],
            check=False, capture_output=True, env=docker_env(), timeout=120,
        )

    def diff_lines(self) -> int:
        """Accumulated changed lines in agent workspace relative to initial git state.

        Uses a hidden GIT_DIR outside /workspace so the agent can't see/modify it.
        """
        script = (
            f"export GIT_DIR=/var/lib/lhb/git; "
            f"export GIT_WORK_TREE={self.workspace_dir}; "
            f"cd {self.workspace_dir} && "
            f"(git diff --numstat HEAD 2>/dev/null; "
            f" git ls-files --others --exclude-standard 2>/dev/null "
            f"  | xargs -I{{}} wc -l {{}} 2>/dev/null "
            f"  | awk '{{print $1\"\\t0\\t\"$2}}'"
            f") | awk 'BEGIN{{s=0}} "
            f"{{if($1!=\"-\" && $1~/^[0-9]+$/){{s+=$1}}; "
            f"if($2!=\"-\" && $2~/^[0-9]+$/){{s+=$2}}}} "
            f"END{{print s}}'"
        )
        cmd = [*docker_command(["exec", self.agent_container, "sh", "-c"]), script]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, env=docker_env(), timeout=60,
            )
        except subprocess.TimeoutExpired:
            return 0
        try:
            return int((r.stdout or "0").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return 0
