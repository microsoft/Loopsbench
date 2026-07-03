#!/usr/bin/env python3
"""Regenerate Django gold patches using cumulative approach.

For each PR, extracts the target file state from git blob objects
referenced in the patch, then generates the diff against the current
workspace state.
"""

import re
import shutil
import subprocess
import tempfile
import os
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
WORKSPACE_ROOT = REPO_ROOT.parent


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


REPO_DIR = _env_path("LHB_DJANGO_REPO_DIR", WORKSPACE_ROOT / "repos" / "django")
TASK_DIR = _env_path("LHB_DJANGO_TASK_DIR", REPO_ROOT / "tasks" / "task_django_seg11")
BASE_DIR = TASK_DIR / "base"

PR_ORDER = ['18127', '18165', '18197', '18172', '18199', '18187', '18195', '18221', '18245', '18252', '18237', '18251', '18120', '18261', '18281', '18278', '18292', '18294', '18307', '18304', '18308', '18313', '18316', '18259', '18321', '18322', '18319', '18339', '18338', '18268', '18309', '18342', '18293', '18345', '18358', '18360', '18366', '18369', '18373', '18376', '18374', '18380', '18370', '18384', '18394', '18353', '18371', '18401', '18403', '18414', '18416', '18423', '18377', '18404', '18433', '18413', '18437', '18431', '18440', '18407', '18425', '18429', '18449', '18442', '18422', '18399', '18435', '18453', '18467', '18356', '18412', '18458', '18434', '18462', '18484', '18469', '18491', '18500', '18505', '18463', '18464', '18498', '18470', '18518', '18346', '18508', '18526', '18496', '18527', '18532', '18536', '18349', '18557', '18572', '18563', '18571', '18569', '18564', '18575', '18584', '18582', '18586', '18547', '18565', '18590', '18598', '18591', '18600', '18605', '18549', '18629', '18654', '18613', '18634', '18656', '18633', '18661', '18653', '18666', '18623', '18555', '18561', '18620', '18681', '18652', '18680', '18684', '18677', '18688', '18631', '18716', '18731', '18735', '18707', '18593', '18741', '18672', '18753', '18747', '18765', '18768', '18759', '18770', '18794', '18795', '18796', '18783', '18792', '18752', '18824', '18820', '18852', '18785', '18816', '18859']


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


def parse_patch_targets(patch_path):
    """Parse a patch file to extract target file paths and blob hashes.

    Returns list of (file_path, before_hash, after_hash, is_new, is_deleted).
    """
    targets = []
    content = patch_path.read_text(errors="replace")

    current_file = None
    is_new = False
    is_deleted = False

    for line in content.split("\n"):
        if line.startswith("diff --git"):
            # Extract b/ path
            m = re.search(r" b/(.+)$", line)
            if m:
                current_file = m.group(1)
                is_new = False
                is_deleted = False
        elif line.startswith("new file"):
            is_new = True
        elif line.startswith("deleted file"):
            is_deleted = True
        elif line.startswith("index ") and current_file:
            # index abcdef1234..abcdef5678 100644
            m = re.match(r"index ([0-9a-f]+)\.\.([0-9a-f]+)", line)
            if m:
                targets.append((current_file, m.group(1), m.group(2), is_new, is_deleted))
                current_file = None

    return targets


def get_blob_content(blob_hash):
    """Get content of a git blob object."""
    r = subprocess.run(
        ["git", "cat-file", "-p", blob_hash],
        cwd=REPO_DIR, capture_output=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout


def generate_patches():
    """Generate cumulative patches."""
    output_dir = TASK_DIR / "gold_patches_new"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir()

    tmpdir = tempfile.mkdtemp(prefix="dj_cumulative_")
    workspace = Path(tmpdir) / "workspace"
    shutil.copytree(BASE_DIR, workspace)

    run_git(["init"], cwd=workspace)
    run_git(["config", "user.email", "bench@test.com"], cwd=workspace)
    run_git(["config", "user.name", "Bench"], cwd=workspace)
    run_git(["config", "core.autocrlf", "false"], cwd=workspace)
    run_git(["add", "-A"], cwd=workspace)
    run_git(["commit", "-m", "base", "--allow-empty"], cwd=workspace)

    gold_dir = TASK_DIR / "gold_patches"
    success = 0
    empty = 0
    regenerated = 0
    kept_original = 0

    for i, pr in enumerate(PR_ORDER):
        patch_file = gold_dir / f"{pr}.diff"
        if not patch_file.exists() or patch_file.stat().st_size == 0:
            (output_dir / f"{pr}.diff").write_text("")
            empty += 1
            run_git(["commit", "--allow-empty", "-m", f"PR #{pr}"], cwd=workspace)
            if i < 10:
                print(f"  [{i+1}/{len(PR_ORDER)}] PR #{pr}: empty")
            continue

        # Try to apply existing patch
        r = run_git(["apply", "--whitespace=nowarn", str(patch_file)], cwd=workspace, check=False)
        if r.returncode == 0:
            # Patch applied cleanly — generate cumulative diff
            run_git(["add", "-A"], cwd=workspace)
            diff_result = run_git(["diff", "--cached", "--binary", "--no-renames"], cwd=workspace, check=False, binary=True)
            patch_content = diff_result.stdout
            if patch_content.strip():
                (output_dir / f"{pr}.diff").write_bytes(patch_content)
                run_git(["commit", "-m", f"PR #{pr}"], cwd=workspace)
                success += 1
            else:
                (output_dir / f"{pr}.diff").write_text("")
                run_git(["commit", "--allow-empty", "-m", f"PR #{pr}"], cwd=workspace)
                empty += 1
        else:
            # Patch failed — try patch -p1 with fuzz
            run_git(["checkout", "."], cwd=workspace, check=False)
            subprocess.run(["git", "clean", "-fd"], cwd=workspace, capture_output=True)

            r2 = subprocess.run(
                ["patch", "-p1", "--forward", "--no-backup-if-mismatch", "--fuzz=3",
                 "-i", str(patch_file)],
                cwd=workspace, capture_output=True, text=True,
            )
            if r2.returncode == 0:
                subprocess.run(["find", ".", "-name", "*.rej", "-delete"], cwd=workspace)
                subprocess.run(["find", ".", "-name", "*.orig", "-delete"], cwd=workspace)
                run_git(["add", "-A"], cwd=workspace)
                diff_result = run_git(["diff", "--cached", "--binary", "--no-renames"], cwd=workspace, check=False, binary=True)
                patch_content = diff_result.stdout
                if patch_content.strip():
                    (output_dir / f"{pr}.diff").write_bytes(patch_content)
                    run_git(["commit", "-m", f"PR #{pr}"], cwd=workspace)
                    regenerated += 1
                    print(f"  [{i+1}/{len(PR_ORDER)}] PR #{pr}: REGENERATED via fuzz")
                else:
                    (output_dir / f"{pr}.diff").write_text("")
                    run_git(["commit", "--allow-empty", "-m", f"PR #{pr}"], cwd=workspace)
                    empty += 1
            else:
                # Try regeneration from blob objects
                run_git(["checkout", "."], cwd=workspace, check=False)
                subprocess.run(["git", "clean", "-fd"], cwd=workspace, capture_output=True)

                targets = parse_patch_targets(patch_file)
                blob_success = False
                if targets:
                    applied_any = False
                    for file_path, before_hash, after_hash, is_new, is_deleted in targets:
                        dst = workspace / file_path
                        if is_deleted:
                            if dst.exists():
                                dst.unlink()
                                applied_any = True
                        elif after_hash and not after_hash.startswith("0000000"):
                            content = get_blob_content(after_hash)
                            if content is not None:
                                dst.parent.mkdir(parents=True, exist_ok=True)
                                dst.write_bytes(content)
                                applied_any = True

                    if applied_any:
                        run_git(["add", "-A"], cwd=workspace)
                        diff_result = run_git(["diff", "--cached", "--binary", "--no-renames"], cwd=workspace, check=False, binary=True)
                        patch_content = diff_result.stdout
                        if patch_content.strip():
                            (output_dir / f"{pr}.diff").write_bytes(patch_content)
                            run_git(["commit", "-m", f"PR #{pr}"], cwd=workspace)
                            regenerated += 1
                            blob_success = True
                            print(f"  [{i+1}/{len(PR_ORDER)}] PR #{pr}: REGENERATED from blobs ({len(targets)} files)")

                if not blob_success:
                    # Keep original patch — the validator's own fallback mechanisms may handle it
                    run_git(["checkout", "."], cwd=workspace, check=False)
                    subprocess.run(["git", "clean", "-fd"], cwd=workspace, capture_output=True)
                    shutil.copy2(patch_file, output_dir / f"{pr}.diff")
                    # Force-apply the target state using blob content from patch hunks
                    # We'll just commit empty and let the original patch handle it
                    run_git(["commit", "--allow-empty", "-m", f"PR #{pr}"], cwd=workspace)
                    kept_original += 1
                    print(f"  [{i+1}/{len(PR_ORDER)}] PR #{pr}: KEPT ORIGINAL (cannot regenerate)")

        if i % 25 == 0 and i > 0:
            print(f"  [{i+1}/{len(PR_ORDER)}] progress: {i+1}/{len(PR_ORDER)}")

    print(f"\nGeneration complete:")
    print(f"  Successful (applied directly): {success}")
    print(f"  Regenerated: {regenerated}")
    print(f"  Kept original: {kept_original}")
    print(f"  Empty: {empty}")

    return output_dir


def verify_patches(output_dir):
    """Verify patches apply cleanly in sequence."""
    print("\n" + "=" * 60)
    print("VERIFICATION: Applying patches in solution.sh order...")
    print("=" * 60)

    tmpdir = tempfile.mkdtemp(prefix="dj_verify_")
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

        r = run_git(["apply", "--whitespace=nowarn", str(patch_file)], cwd=workspace, check=False)
        if r.returncode != 0:
            r2 = subprocess.run(
                ["patch", "-p1", "--forward", "--no-backup-if-mismatch", "--fuzz=3",
                 "-i", str(patch_file)],
                cwd=workspace, capture_output=True, text=True,
            )
            if r2.returncode != 0:
                print(f"  FAIL: PR #{pr}")
                print(f"    git apply: {r.stderr[:200]}")
                failed += 1
                run_git(["checkout", "."], cwd=workspace, check=False)
                subprocess.run(["find", ".", "-name", "*.rej", "-delete"], cwd=workspace)
                run_git(["commit", "--allow-empty", "-m", f"PR #{pr}"], cwd=workspace)
                continue
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
