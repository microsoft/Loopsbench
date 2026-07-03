#!/usr/bin/env python3
"""Regenerate Framework gold patches using cumulative approach.

Generates patches that reflect the diff relative to workspace state AFTER
all prior patches have been applied (not standalone from commit).
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
WORKSPACE_ROOT = REPO_ROOT.parent


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


REPO_DIR = _env_path("LHB_FRAMEWORK_REPO_DIR", WORKSPACE_ROOT / "repos" / "framework")
TASK_DIR = _env_path("LHB_FRAMEWORK_TASK_DIR", REPO_ROOT / "tasks" / "task_framework_seg04")
BASE_DIR = TASK_DIR / "base"
BASE_COMMIT = "b5f386196598ec25cbb2b993ecc613aa047bd918"

PR_ORDER = ['45947', '45951', '45950', '45949', '45956', '45957', '45960', '45958', '45967', '45954', '45974', '45973', '45968', '45982', '45985', '45991', '46002', '45998', '46001', '45993', '45990', '45989', '46006', '45969', '45963', '46009', '46010', '46016', '46026', '46025', '46021', '46017', '46014', '46015', '46034', '46033', '46035', '46038', '45988', '45977', '46047', '46055', '46052', '46049', '46043', '46053', '46066', '46073', '46076', '46075', '46063', '46081', '46079', '46089', '46096', '46095', '46098', '46011', '46102', '46112', '46130', '46127', '46122', '46119', '46137', '46136', '46135', '46144', '46132', '46142', '46146', '46105', '46155', '46153', '46152', '46156', '46158', '46166', '46173', '46181', '46187', '46186', '46196', '46184', '46200', '46203', '46206', '46201', '46217', '46160', '46223', '46188', '46232', '46228', '46241', '46215', '46259', '46265', '46281', '46303', '46285', '46279', '46328', '46319', '46326', '46329', '46271', '46351', '46356', '46360', '46349', '46361', '46366', '46346', '46336', '46344', '46309', '46392', '46395', '46402', '46406', '46408', '46407', '46403', '46418', '46420', '46411', '46428', '46432', '46426', '46429', '46445', '46378', '46442', '46443', '46460', '46461', '46483', '46481', '46489', '46500', '46498', '46488', '46505', '46508', '46513', '46511', '46531', '46529', '46536', '46538', '46517', '46549', '46548', '46565', '46561', '46566', '46552', '46581', '46579', '46575', '46410', '46415', '46569', '46594', '46555', '46559', '46578', '46605', '46626', '46622', '46592', '46635', '46639', '46621', '46660', '46653', '46647', '46658', '46659', '46619', '46678', '46677', '46683', '46691', '46692', '46697', '46696', '46694', '46689', '46704', '46713', '46715', '46726', '46712', '46720', '46716', '46745', '46746', '46752', '46755', '46757', '46768', '46780', '46786', '46787', '46802', '46800', '46809', '46806', '46811', '46821', '46822', '46824', '46841', '46833', '46846', '46848', '46857', '46859', '46869', '46872', '46854', '46876', '46926', '46929', '46914', '46925', '46912', '46922', '46935', '46942', '46945', '46904', '46960', '46961', '46955', '46947', '46963', '46964', '46972', '46989', '46968', '46994', '46992', '46998', '47000', '47004', '47002', '47017', '47018', '47007', '46987', '47029', '47031', '47043', '47055', '47059', '47056', '47047', '47048', '47046', '47062', '47065', '47061', '47069', '47075', '47094', '47091', '47084', '47081', '47083', '47099', '47068', '47114', '47127', '47122', '47098', '47144', '47142', '47159', '47141', '47140', '47161', '47185', '47186', '47193', '47189', '47200', '47201', '47197', '47235', '47228', '47225', '47250', '47264', '47244', '47242', '47285', '47280', '47277', '47229', '47223', '47287', '47210', '47307', '47308', '47227', '47326', '47316', '47310', '47331', '47328', '47327', '47335', '47345', '47347', '47352', '47349', '47348', '47351', '47354', '47365', '47367', '47344', '47376', '47378', '47368', '47380', '47382', '47383', '47392', '47398', '47408', '47414', '47413', '47422', '47375', '47438', '47437', '47436', '47446', '47445', '47434', '47463', '47417', '47478', '47474', '47477', '47479', '47465', '47423', '47404', '47371', '47483', '47500', '47522', '47519', '47517', '47488', '47524', '47297', '47533', '47530', '47540', '47548', '47549', '47553', '47552', '47556', '47557', '47559', '47562', '47561', '47566', '47569', '47567', '47554', '47525']


def run_git(args, cwd=None, check=True, binary=False):
    r = subprocess.run(
        ["git"] + args,
        cwd=cwd or REPO_DIR,
        capture_output=True,
        text=not binary,
    )
    if check and r.returncode != 0:
        stderr = r.stderr if isinstance(r.stderr, str) else r.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"git {' '.join(args)} failed: {stderr[:500]}")
    return r


def build_commit_map_fast():
    """Build PR→commit map in one git log pass."""
    import re
    pr_set = set(PR_ORDER)
    r = subprocess.run(
        ["git", "log", "--oneline", "--all", "--format=%H %s"],
        cwd=REPO_DIR, capture_output=True, text=True,
    )
    commit_map = {}
    for line in r.stdout.split("\n"):
        if not line:
            continue
        m = re.search(r"#(\d+)\)", line)
        if m and m.group(1) in pr_set:
            pr_num = m.group(1)
            commit_hash = line.split(" ", 1)[0]
            if pr_num not in commit_map:
                commit_map[pr_num] = commit_hash
    return commit_map


def get_pr_files(commit_hash):
    """Get the list of files changed in a PR commit."""
    r = run_git(["diff-tree", "--no-commit-id", "-r", "--name-only", commit_hash])
    return [f for f in r.stdout.strip().split("\n") if f]


def generate_patches():
    """Generate cumulative patches."""
    output_dir = TASK_DIR / "gold_patches_new"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir()

    tmpdir = tempfile.mkdtemp(prefix="fw_cumulative_")
    workspace = Path(tmpdir) / "workspace"
    shutil.copytree(BASE_DIR, workspace)

    run_git(["init"], cwd=workspace)
    run_git(["config", "user.email", "bench@test.com"], cwd=workspace)
    run_git(["config", "user.name", "Bench"], cwd=workspace)
    run_git(["config", "core.autocrlf", "false"], cwd=workspace)
    run_git(["add", "-A"], cwd=workspace)
    run_git(["commit", "-m", "base", "--allow-empty"], cwd=workspace)

    print("Building commit map...")
    commit_map = build_commit_map_fast()
    print(f"  Found {len(commit_map)}/{len(PR_ORDER)} PR commits")

    success = 0
    empty = 0
    failed = 0

    for i, pr in enumerate(PR_ORDER):
        commit_hash = commit_map.get(pr)
        if not commit_hash:
            print(f"  [{i+1}/{len(PR_ORDER)}] PR #{pr}: NOT FOUND in repo")
            # Write empty patch
            (output_dir / f"{pr}.diff").write_text("")
            empty += 1
            run_git(["commit", "--allow-empty", "-m", f"PR #{pr}"], cwd=workspace)
            continue

        changed_files = get_pr_files(commit_hash)
        if not changed_files:
            (output_dir / f"{pr}.diff").write_text("")
            empty += 1
            run_git(["commit", "--allow-empty", "-m", f"PR #{pr}"], cwd=workspace)
            if i % 50 == 0 or i < 10:
                print(f"  [{i+1}/{len(PR_ORDER)}] PR #{pr}: empty (no files)")
            continue

        # Apply PR changes to workspace: copy final file state from commit
        for fpath in changed_files:
            dst = workspace / fpath
            pr_content = subprocess.run(
                ["git", "show", f"{commit_hash}:{fpath}"],
                cwd=REPO_DIR, capture_output=True,
            )
            if pr_content.returncode != 0:
                # File was deleted in this PR
                if dst.exists():
                    dst.unlink()
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(pr_content.stdout)

        # Stage all changes
        run_git(["add", "-A"], cwd=workspace)

        # Generate diff from staged changes
        diff_result = run_git(["diff", "--cached", "--binary", "--no-renames"], cwd=workspace, check=False, binary=True)
        patch_content = diff_result.stdout

        if not patch_content.strip():
            (output_dir / f"{pr}.diff").write_text("")
            empty += 1
            run_git(["commit", "--allow-empty", "-m", f"PR #{pr}"], cwd=workspace)
        else:
            (output_dir / f"{pr}.diff").write_bytes(patch_content)
            run_git(["commit", "-m", f"PR #{pr}"], cwd=workspace)
            success += 1

        if i % 50 == 0 or i < 10:
            print(f"  [{i+1}/{len(PR_ORDER)}] PR #{pr}: OK ({len(changed_files)} files)")

    print(f"\nGeneration complete:")
    print(f"  Successful: {success}")
    print(f"  Empty: {empty}")
    print(f"  Failed: {failed}")

    return output_dir


def verify_patches(output_dir):
    """Verify patches apply cleanly in sequence."""
    print("\n" + "=" * 60)
    print("VERIFICATION: Applying patches in solution.sh order...")
    print("=" * 60)

    tmpdir = tempfile.mkdtemp(prefix="fw_verify_")
    workspace = Path(tmpdir) / "workspace"
    shutil.copytree(BASE_DIR, workspace)
    run_git(["init"], cwd=workspace)
    run_git(["config", "user.email", "bench@test.com"], cwd=workspace)
    run_git(["config", "user.name", "Bench"], cwd=workspace)
    run_git(["add", "-A"], cwd=workspace)
    run_git(["commit", "-m", "base"], cwd=workspace)

    applied = 0
    failed = 0
    empty_count = 0

    for pr in PR_ORDER:
        patch_file = output_dir / f"{pr}.diff"
        if not patch_file.exists() or patch_file.stat().st_size == 0:
            run_git(["commit", "--allow-empty", "-m", f"PR #{pr}"], cwd=workspace)
            empty_count += 1
            continue

        # Try git apply
        r = run_git(["apply", "--whitespace=nowarn", str(patch_file)], cwd=workspace, check=False)
        if r.returncode != 0:
            # Try patch -p1 --fuzz=3
            r2 = subprocess.run(
                ["patch", "-p1", "--forward", "--no-backup-if-mismatch", "--fuzz=3",
                 "-i", str(patch_file)],
                cwd=workspace, capture_output=True, text=True,
            )
            if r2.returncode != 0:
                print(f"  FAIL: PR #{pr}")
                print(f"    git apply: {r.stderr[:200]}")
                print(f"    patch: {r2.stderr[:200]}")
                failed += 1
                run_git(["checkout", "."], cwd=workspace, check=False)
                subprocess.run(["find", ".", "-name", "*.rej", "-delete"], cwd=workspace)
                run_git(["commit", "--allow-empty", "-m", f"PR #{pr}"], cwd=workspace)
                continue
            # Clean .rej/.orig files
            subprocess.run(["find", ".", "-name", "*.rej", "-delete"], cwd=workspace)
            subprocess.run(["find", ".", "-name", "*.orig", "-delete"], cwd=workspace)

        run_git(["add", "-A"], cwd=workspace)
        run_git(["commit", "-m", f"PR #{pr}", "--allow-empty"], cwd=workspace)
        applied += 1

    print(f"\nVerification results:")
    print(f"  Applied: {applied}/{len(PR_ORDER)}")
    print(f"  (of which empty: {empty_count})")
    print(f"  Failed: {failed}")

    shutil.rmtree(tmpdir)
    return failed == 0


if __name__ == "__main__":
    output_dir = generate_patches()
    if verify_patches(output_dir):
        print("\nAll patches apply cleanly! Replacing gold_patches...")
        old_dir = TASK_DIR / "gold_patches"
        backup_dir = TASK_DIR / "gold_patches_backup"
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        if old_dir.exists():
            old_dir.rename(backup_dir)
        output_dir.rename(old_dir)
        print("Done! Old patches backed up to gold_patches_backup/")
    else:
        print(f"\nSome patches failed! New patches at: {output_dir}")
        print("NOT replacing gold_patches.")
