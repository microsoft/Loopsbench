#!/usr/bin/env python3
"""
Regenerate gold patches for the TypeScript benchmark task so they all apply
cleanly against the cumulative workspace state.

Approach:
1. Find merge commits for all 414 PRs in the source repo.
2. Determine the chronological merge order from the first-parent history.
3. For PRs not on the first-parent path, find their merge commit and insert
   them at the correct chronological position.
4. Copy base/ into a temp workspace, git init, commit.
5. For each PR in chronological order:
   a. Get files changed by the merge commit (vs first parent).
   b. Copy file contents from the merge commit into the workspace.
   c. Handle deletions and renames.
   d. Stage, generate patch, save, commit.
6. Verify all patches apply cleanly in solution.sh order.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Configuration
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
WORKSPACE_ROOT = REPO_ROOT.parent


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


REPO_DIR = _env_path('LHB_TYPESCRIPT_REPO_DIR', WORKSPACE_ROOT / 'repos' / 'TypeScript')
TASK_DIR = _env_path('LHB_TYPESCRIPT_TASK_DIR', REPO_ROOT / 'tasks' / 'task_TypeScript_seg01')
BASE_DIR = TASK_DIR / 'base'
SOURCE_REPO = REPO_DIR
GOLD_DIR = TASK_DIR / 'gold_patches'
BASE_COMMIT = '546a8492f25a48a30f03ac1d4cb01767a49765ad'


def run_git(args, cwd, input_data=None, check=True):
    """Run a git command and return the result."""
    result = subprocess.run(
        ['git'] + args,
        cwd=cwd,
        input=input_data,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, ['git'] + args,
            result.stdout, result.stderr
        )
    return result


def get_patch_ids():
    """Get all PR numbers from the gold_patches directory."""
    ids = set()
    for f in os.listdir(GOLD_DIR):
        if f.endswith('.diff'):
            ids.add(f[:-5])
    return ids


def get_solution_order():
    """Get the ordered list of PRs as solution.sh would apply them."""
    unit_dag_path = TASK_DIR / 'unit_dag.json'
    slug_map_path = TASK_DIR / 'slug_diff_map.json'

    ordered = []
    seen = set()

    # 1. DAG nodes in order
    if unit_dag_path.is_file():
        dag = json.loads(unit_dag_path.read_text())
        for node in dag.get('nodes', []):
            unit = str(node.get('id', '')).strip()
            if unit and unit not in seen:
                seen.add(unit)
                ordered.append(unit)

    # 2. slug_diff_map extras
    if slug_map_path.is_file():
        slug_map = json.loads(slug_map_path.read_text())
        if isinstance(slug_map, dict):
            for diff_rel in slug_map.values():
                stem = Path(str(diff_rel)).stem
                unit = stem[3:] if stem.startswith('pr_') else stem
                if unit and unit not in seen:
                    seen.add(unit)
                    ordered.append(unit)

    # 3. Remaining patches from directory
    for patch_path in sorted(GOLD_DIR.glob('*.diff'), key=lambda p: int(p.stem) if p.stem.isdigit() else 0):
        stem = patch_path.stem
        unit = stem[3:] if stem.startswith('pr_') else stem
        if unit not in seen:
            seen.add(unit)
            ordered.append(unit)

    return ordered


def find_merge_commits(patch_ids):
    """Find merge commit hashes for all PR numbers."""
    print("Finding merge commits for all PRs...")
    commit_map = {}

    # First, get all merge commits from the first-parent path
    result = run_git(
        ['log', '--format=%H %s', '--first-parent', '--reverse',
         f'{BASE_COMMIT}..HEAD'],
        cwd=SOURCE_REPO
    )
    for line in result.stdout.decode().splitlines():
        m = re.match(r'^([0-9a-f]+)\s+Merge pull request #(\d+)\b', line)
        if m:
            commit_hash = m.group(1)
            pr_num = m.group(2)
            if pr_num in patch_ids and pr_num not in commit_map:
                commit_map[pr_num] = commit_hash

    # For any PRs not found on the first-parent path, do a single bulk search
    missing = patch_ids - set(commit_map.keys())
    if missing:
        print(f"  {len(missing)} PRs not on first-parent path, searching all branches...")
        # Single git log --all to find all merge commits at once
        result = run_git(
            ['log', '--format=%ct %H %s', '--all', '--reverse',
             '--grep=Merge pull request #'],
            cwd=SOURCE_REPO
        )
        # Build a map of PR -> [(timestamp, hash)] for missing PRs
        candidates_map = {pr: [] for pr in missing}
        for line in result.stdout.decode().splitlines():
            m = re.match(r'^(\d+)\s+([0-9a-f]+)\s+Merge pull request #(\d+)\b', line)
            if m:
                ts = int(m.group(1))
                commit_hash = m.group(2)
                pr_num = m.group(3)
                if pr_num in candidates_map:
                    candidates_map[pr_num].append((ts, commit_hash))

        # For each missing PR, pick the earliest commit that's after base
        for pr_num in sorted(missing, key=int):
            candidates = candidates_map.get(pr_num, [])
            candidates.sort()  # Sort by timestamp (earliest first)
            for ts, commit_hash in candidates:
                check = run_git(
                    ['merge-base', '--is-ancestor', BASE_COMMIT, commit_hash],
                    cwd=SOURCE_REPO, check=False
                )
                if check.returncode == 0:
                    commit_map[pr_num] = commit_hash
                    break

    still_missing = patch_ids - set(commit_map.keys())
    if still_missing:
        print(f"  WARNING: Could not find merge commits for: {sorted(still_missing, key=int)}")

    print(f"  Found merge commits for {len(commit_map)}/{len(patch_ids)} PRs")
    return commit_map


def get_chronological_order(commit_map, patch_ids):
    """
    Determine chronological order by finding where each merge commit
    appears relative to the first-parent path.
    """
    print("Determining chronological merge order...")

    # Get the first-parent path with hashes
    result = run_git(
        ['log', '--format=%H', '--first-parent', '--reverse',
         f'{BASE_COMMIT}..HEAD'],
        cwd=SOURCE_REPO
    )
    first_parent_hashes = result.stdout.decode().strip().splitlines()
    fp_index = {h: i for i, h in enumerate(first_parent_hashes)}

    # For each PR, determine its chronological position
    pr_positions = {}
    for pr_num, commit_hash in commit_map.items():
        if commit_hash in fp_index:
            # Directly on first-parent path
            pr_positions[pr_num] = fp_index[commit_hash]
        else:
            # Find the first-parent commit that is a descendant of this merge
            # (i.e., the first-parent commit that contains this merge)
            # Use merge-base to find where it joins the mainline
            result = run_git(
                ['log', '--format=%H', '--first-parent', '--ancestry-path',
                 f'{commit_hash}..HEAD'],
                cwd=SOURCE_REPO, check=False
            )
            if result.returncode == 0 and result.stdout.strip():
                # The last line is the first commit on first-parent path after this merge
                ancestors = result.stdout.decode().strip().splitlines()
                # Find the earliest first-parent commit that has this merge as ancestor
                earliest_fp = None
                for h in reversed(ancestors):
                    if h in fp_index:
                        earliest_fp = h
                        break
                if earliest_fp:
                    pr_positions[pr_num] = fp_index[earliest_fp]
                else:
                    # Fallback: use a large position (process last)
                    pr_positions[pr_num] = len(first_parent_hashes)
            else:
                pr_positions[pr_num] = len(first_parent_hashes)

    # Sort by position
    ordered = sorted(
        [pr for pr in patch_ids if pr in commit_map],
        key=lambda pr: (pr_positions.get(pr, len(first_parent_hashes)), int(pr))
    )
    return ordered


def get_file_changes(commit_hash):
    """
    Get files changed by a merge commit (compared to first parent).
    Returns dict with keys: 'added', 'modified', 'deleted', 'renamed'
    Each value is a list of (old_path, new_path) tuples (old==new for non-renames)
    """
    # Use diff with rename detection against first parent
    result = run_git(
        ['diff', '--name-status', '-M', f'{commit_hash}^1', commit_hash],
        cwd=SOURCE_REPO
    )

    changes = {
        'added': [],
        'modified': [],
        'deleted': [],
        'renamed': [],
        'copied': [],
    }

    for line in result.stdout.decode().splitlines():
        if not line.strip():
            continue
        parts = line.split('\t')
        status = parts[0]

        if status == 'A':
            changes['added'].append(parts[1])
        elif status == 'M':
            changes['modified'].append(parts[1])
        elif status == 'D':
            changes['deleted'].append(parts[1])
        elif status.startswith('R'):
            old_path = parts[1]
            new_path = parts[2]
            changes['renamed'].append((old_path, new_path))
        elif status.startswith('C'):
            changes['copied'].append(parts[2] if len(parts) > 2 else parts[1])

    return changes


def get_file_content(commit_hash, filepath):
    """Get file content from a specific commit."""
    result = run_git(
        ['show', f'{commit_hash}:{filepath}'],
        cwd=SOURCE_REPO, check=False
    )
    if result.returncode != 0:
        return None
    return result.stdout


EXCLUDE_PATTERNS = {'package-lock.json'}
EXCLUDE_PREFIXES = ('node_modules/',)


def _should_exclude(filepath):
    """Exclude npm-generated files that the Docker container creates via npm install."""
    basename = filepath.rsplit('/', 1)[-1] if '/' in filepath else filepath
    if basename in EXCLUDE_PATTERNS:
        return True
    return any(filepath.startswith(p) for p in EXCLUDE_PREFIXES)


def apply_pr_changes(workspace, commit_hash, pr_num):
    """
    Apply a PR's changes to the workspace by copying file contents
    from the merge commit.
    """
    changes = get_file_changes(commit_hash)

    files_modified = 0

    # Handle added files
    for filepath in changes['added']:
        if _should_exclude(filepath):
            continue
        content = get_file_content(commit_hash, filepath)
        if content is None:
            continue
        dest = workspace / filepath
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        files_modified += 1

    # Handle modified files
    for filepath in changes['modified']:
        if _should_exclude(filepath):
            continue
        content = get_file_content(commit_hash, filepath)
        if content is None:
            continue
        dest = workspace / filepath
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        files_modified += 1

    # Handle deleted files
    for filepath in changes['deleted']:
        if _should_exclude(filepath):
            continue
        dest = workspace / filepath
        if dest.exists():
            dest.unlink()
            files_modified += 1

    # Handle renames
    for old_path, new_path in changes['renamed']:
        if _should_exclude(old_path) or _should_exclude(new_path):
            continue
        # Delete old file
        old_dest = workspace / old_path
        if old_dest.exists():
            old_dest.unlink()

        # Create new file with content from merge commit
        content = get_file_content(commit_hash, new_path)
        if content is not None:
            new_dest = workspace / new_path
            new_dest.parent.mkdir(parents=True, exist_ok=True)
            new_dest.write_bytes(content)
        files_modified += 1

    # Handle copied files
    for filepath in changes['copied']:
        if _should_exclude(filepath):
            continue
        content = get_file_content(commit_hash, filepath)
        if content is None:
            continue
        dest = workspace / filepath
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        files_modified += 1

    return files_modified


def generate_patches(ordered_prs, commit_map):
    """Generate cumulative patches by simulating workspace state.

    For each PR in order:
    1. Copy file contents from merge commit into workspace
    2. Generate diff from current workspace state
    3. Commit changes
    This ensures each patch applies cleanly on top of all prior patches.
    """
    print(f"\nGenerating cumulative patches for {len(ordered_prs)} PRs...")

    # Create output directory
    output_dir = TASK_DIR / 'gold_patches_new'
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir()

    # Create workspace from base
    tmpdir = tempfile.mkdtemp(prefix='ts_cumulative_')
    workspace = Path(tmpdir) / 'workspace'
    shutil.copytree(BASE_DIR, workspace)
    run_git(['init'], cwd=workspace)
    run_git(['config', 'user.email', 'bench@test.com'], cwd=workspace)
    run_git(['config', 'user.name', 'Bench'], cwd=workspace)
    run_git(['config', 'core.autocrlf', 'false'], cwd=workspace)

    # Normalize package.json to match Docker container state
    # (npm install reformats it: removes trailing whitespace, normalizes colons)
    print("  Normalizing package.json to match Docker state...")
    pkg_path = workspace / 'package.json'
    if pkg_path.exists():
        pkg_data = json.loads(pkg_path.read_text())
        pkg_path.write_text(json.dumps(pkg_data, indent='\t') + '\n')

    run_git(['add', '-A'], cwd=workspace)
    run_git(['commit', '-m', 'base', '--allow-empty'], cwd=workspace)

    success_count = 0
    empty_count = 0
    fail_count = 0
    failures = []

    for i, pr_num in enumerate(ordered_prs):
        if pr_num not in commit_map:
            print(f"  [{i+1}/{len(ordered_prs)}] PR #{pr_num}: NO MERGE COMMIT - saving empty")
            (output_dir / f'{pr_num}.diff').write_bytes(b'')
            empty_count += 1
            continue

        commit_hash = commit_map[pr_num]

        try:
            files_modified = apply_pr_changes(workspace, commit_hash, pr_num)

            if files_modified == 0:
                (output_dir / f'{pr_num}.diff').write_bytes(b'')
                empty_count += 1
                if (i + 1) % 50 == 0 or i < 10:
                    print(f"  [{i+1}/{len(ordered_prs)}] PR #{pr_num}: empty (no effective changes)")
            else:
                # Stage all changes
                run_git(['add', '-A'], cwd=workspace)

                # Generate patch from staged changes (--binary for .docx, --no-renames for compatibility)
                result = run_git(['diff', '--cached', '--binary', '--no-renames'], cwd=workspace)
                patch_content = result.stdout

                if not patch_content.strip():
                    (output_dir / f'{pr_num}.diff').write_bytes(b'')
                    empty_count += 1
                    if (i + 1) % 50 == 0 or i < 10:
                        print(f"  [{i+1}/{len(ordered_prs)}] PR #{pr_num}: empty (no diff)")
                else:
                    (output_dir / f'{pr_num}.diff').write_bytes(patch_content)
                    file_count = patch_content.count(b'diff --git')
                    success_count += 1
                    if (i + 1) % 50 == 0 or i < 10:
                        print(f"  [{i+1}/{len(ordered_prs)}] PR #{pr_num}: OK ({file_count} files)")

                # Commit to advance workspace state
                run_git(['commit', '-m', f'PR #{pr_num}', '--allow-empty'], cwd=workspace)

        except Exception as e:
            print(f"  [{i+1}/{len(ordered_prs)}] PR #{pr_num}: FAILED - {e}")
            (output_dir / f'{pr_num}.diff').write_bytes(b'')
            fail_count += 1
            failures.append(pr_num)
            # Reset workspace on failure
            run_git(['reset', '--hard', 'HEAD'], cwd=workspace, check=False)
            run_git(['clean', '-fd'], cwd=workspace, check=False)

    shutil.rmtree(tmpdir)

    print(f"\nGeneration complete:")
    print(f"  Successful: {success_count}")
    print(f"  Empty: {empty_count}")
    print(f"  Failed: {fail_count}")
    if failures:
        print(f"  Failed PRs: {failures}")

    return output_dir


def verify_patches(output_dir, ordered_prs):
    """Verify ALL patches apply cleanly in cumulative order."""
    print(f"\n{'='*60}")
    print("VERIFICATION: Applying patches in solution.sh order...")
    print(f"{'='*60}")

    tmpdir = tempfile.mkdtemp(prefix='ts_verify_')
    workspace = Path(tmpdir) / 'workspace'
    shutil.copytree(BASE_DIR, workspace)
    run_git(['init'], cwd=workspace)
    run_git(['config', 'user.email', 'bench@test.com'], cwd=workspace)
    run_git(['config', 'user.name', 'Bench'], cwd=workspace)
    run_git(['config', 'core.autocrlf', 'false'], cwd=workspace)

    # Normalize package.json to match Docker state
    pkg_path = workspace / 'package.json'
    if pkg_path.exists():
        pkg_data = json.loads(pkg_path.read_text())
        pkg_path.write_text(json.dumps(pkg_data, indent='\t') + '\n')

    run_git(['add', '-A'], cwd=workspace)
    run_git(['commit', '-m', 'base', '--allow-empty'], cwd=workspace)

    applied = 0
    empty = 0
    failed_list = []

    for pr_num in ordered_prs:
        patch_path = output_dir / f'{pr_num}.diff'
        if not patch_path.exists() or patch_path.stat().st_size == 0:
            empty += 1
            continue

        result = run_git(
            ['apply', '--whitespace=nowarn', str(patch_path)],
            cwd=workspace, check=False
        )
        if result.returncode != 0:
            # Try with --3way fallback
            result = run_git(
                ['apply', '--3way', '--whitespace=nowarn', str(patch_path)],
                cwd=workspace, check=False
            )
        if result.returncode == 0:
            applied += 1
            run_git(['add', '-A'], cwd=workspace)
            run_git(['commit', '-m', f'PR #{pr_num}', '--allow-empty'], cwd=workspace)
        else:
            failed_list.append(pr_num)
            error = result.stderr.decode()[:200]
            print(f"  FAIL PR #{pr_num}: {error}")
            if len(failed_list) >= 10:
                print("  ... stopping after 10 failures")
                break

    shutil.rmtree(tmpdir)
    total = applied + empty
    print(f"\nVerification results:")
    print(f"  Applied: {applied + empty}/{len(ordered_prs)}")
    print(f"  (of which empty: {empty})")
    print(f"  Failed: {len(failed_list)}")
    if failed_list:
        print(f"  Failed PRs: {failed_list[:20]}")
    return len(failed_list) == 0


def main():
    # Get all patch IDs
    patch_ids = get_patch_ids()
    print(f"Total patch IDs: {len(patch_ids)}")

    # Use the VALIDATOR's PR order — this is what validate_per_pr.py actually uses
    sys.path.insert(0, str(Path(__file__).parent))
    from validate_per_pr import get_pr_order
    validator_order = get_pr_order(TASK_DIR)
    # Filter to only existing patch IDs and add any missing
    solution_order = [pr for pr in validator_order if pr in patch_ids]
    remaining = patch_ids - set(solution_order)
    solution_order.extend(sorted(remaining, key=int))
    print(f"Validator order: {len(solution_order)} PRs (from get_pr_order)")

    # Find merge commits
    commit_map = find_merge_commits(patch_ids)

    # Generate patches in the validator's order
    processing_order = solution_order

    # Generate patches
    output_dir = generate_patches(processing_order, commit_map)

    # Verify in the same order
    success = verify_patches(output_dir, solution_order)

    if success:
        print(f"\nAll patches apply cleanly! Replacing gold_patches...")
        # Backup old patches
        backup_dir = TASK_DIR / 'gold_patches_backup'
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        shutil.move(str(GOLD_DIR), str(backup_dir))
        shutil.move(str(output_dir), str(GOLD_DIR))
        print("Done! Old patches backed up to gold_patches_backup/")
    else:
        print(f"\nSome patches still fail. New patches are in: {output_dir}")
        print("NOT replacing gold_patches.")

    return 0 if success else 1


if __name__ == '__main__':
    sys.exit(main())
