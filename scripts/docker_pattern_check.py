#!/usr/bin/env python3
"""Find surviving patterns by checking inside Docker.

Runs the pattern check inside the Docker container to match the Oracle environment exactly.
Outputs surviving patterns as JSON for test regeneration.

Usage:
    python scripts/docker_pattern_check.py <task_name> --prs pr1,pr2,...
"""

import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


def parse_diff(diff_path):
    result = defaultdict(lambda: {"added": [], "removed": []})
    current = None
    with open(diff_path, "r", errors="replace") as f:
        for line in f:
            if line.startswith("+++ b/"):
                current = line[6:].strip()
            elif current and line.startswith("+") and not line.startswith("+++"):
                result[current]["added"].append(line[1:].rstrip("\n"))
            elif current and line.startswith("-") and not line.startswith("---"):
                result[current]["removed"].append(line[1:].rstrip("\n"))
    return dict(result)


SKIP_LINE = re.compile(
    r"^\s*$|^\s*//|^\s*/?\*|^\s*import\b|^\s*require\(|^\s*export\s+{|"
    r"^\s*[{}];?\s*$|^\s*\)|^\s*\];?\s*$|^\s*return;?\s*$|^\s*break;?\s*$|"
    r"^\s*else\s*{?\s*$|^\s*}\s*else"
)


def score_line(line):
    stripped = line.strip()
    if not stripped or len(stripped) < 6:
        return 0
    if SKIP_LINE.match(stripped):
        return 0
    score = len(stripped)
    if re.search(r"\b(function|class|interface|type|enum)\s+\w+", stripped):
        score += 200
    if re.search(r"\b(export\s+(function|class|const|let|var|type|interface))\b", stripped):
        score += 150
    if re.search(r"\w+\s*\([^)]*\)\s*[:{]", stripped):
        score += 80
    if re.search(r"\w+\s*[:=]\s*['\"]", stripped):
        score += 60
    if re.search(r"[a-z][A-Z]", stripped):
        score += 30
    if re.match(r"^\s*(if|else|for|while|switch|case|try|catch)\b", stripped):
        score -= 50
    if stripped in ("};", "});", ");", "},"):
        return 0
    return max(score, 0)


def file_priority(fpath):
    if fpath.endswith((".map", ".min.js", ".min.css")):
        return -1000
    if "/dist/" in fpath or fpath.startswith("dist/"):
        return -500
    if fpath.startswith("src/") or "/src/" in fpath:
        return 200
    if fpath.endswith((".ts", ".tsx")):
        return 150
    if fpath.endswith((".tpl", ".html")):
        return 100
    if fpath.endswith(".js"):
        return 60
    return 10


def main():
    task_name = sys.argv[1]
    prs = sys.argv[2].split(",")
    task_dir = Path("tasks") / task_name

    docker_tag = f"lhb-patterncheck-{task_name.replace('_', '-').lower()}"

    # Build Docker image
    print(f"Building Docker image for {task_name}...")
    r = subprocess.run(
        ["sg", "docker", "-c", f"docker build -q -t {docker_tag} tasks/{task_name}"],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        print(f"Docker build failed: {r.stderr}")
        sys.exit(1)

    # Get file content from Docker (after applying all patches)
    # We build a script that applies patches and dumps file contents
    check_script = """#!/bin/bash
cd /workspace
bash /sol.sh 2>&1 >/dev/null

# For each file requested, output its content
while IFS= read -r fpath; do
    if [ -f "$fpath" ]; then
        echo "FILE:$fpath"
        cat "$fpath"
        echo "ENDFILE:$fpath"
    else
        echo "MISSING:$fpath"
    fi
done
"""

    # Collect all candidate files from diffs
    all_files = set()
    pr_diffs = {}
    for pr in prs:
        diff_path = task_dir / "gold_patches" / f"{pr}.diff"
        if diff_path.exists():
            diff = parse_diff(diff_path)
            pr_diffs[pr] = diff
            for fpath in diff:
                if "/test/" not in fpath and not fpath.startswith("test/"):
                    if not fpath.endswith((".json", ".md", ".yml", ".yaml", ".lock", ".txt", ".map")):
                        all_files.add(fpath)

    # Get base file contents
    base_dir = task_dir / "base"
    base_contents = {}
    for fpath in all_files:
        bf = base_dir / fpath
        if bf.exists():
            base_contents[fpath] = bf.read_text(errors="replace")

    # Get Docker final state file contents
    file_list = "\n".join(sorted(all_files))
    cmd = [
        "sg", "docker", "-c",
        f"docker run --rm "
        f"-v {task_dir.resolve()}/gold_patches:/workspace/gold_patches:ro "
        f"-v {task_dir.resolve()}/solution.sh:/sol.sh:ro "
        f"{docker_tag} bash -c '"
        f"cd /workspace && bash /sol.sh 2>&1 >/dev/null; "
        f"for f in {' '.join(sorted(all_files))}; do "
        f"  if [ -f \"$f\" ]; then echo \"===FILE:$f===\"; cat \"$f\"; echo \"===ENDFILE:$f===\"; "
        f"  else echo \"===MISSING:$f===\"; fi; done'"
    ]

    print(f"Checking {len(all_files)} files in Docker final state...")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    output = r.stdout

    # Parse Docker file contents
    docker_contents = {}
    for fpath in all_files:
        start_marker = f"===FILE:{fpath}==="
        end_marker = f"===ENDFILE:{fpath}==="
        start_idx = output.find(start_marker)
        if start_idx == -1:
            continue
        start_idx += len(start_marker) + 1  # skip newline
        end_idx = output.find(end_marker, start_idx)
        if end_idx == -1:
            continue
        docker_contents[fpath] = output[start_idx:end_idx]

    print(f"Got {len(docker_contents)} files from Docker")

    # Find surviving patterns for each PR
    results = {}
    for pr in prs:
        if pr not in pr_diffs:
            results[pr] = {"status": "no_diff"}
            continue

        diff = pr_diffs[pr]
        candidates = []

        for fpath, changes in diff.items():
            if "/test/" in fpath or fpath.startswith("test/"):
                continue
            if fpath.endswith((".json", ".md", ".yml", ".yaml", ".lock", ".txt", ".map")):
                continue
            prio = file_priority(fpath)
            if prio <= -500:
                continue

            docker_content = docker_contents.get(fpath, "")
            base_content = base_contents.get(fpath, "")

            if not docker_content:
                continue

            # Check added lines (positive patterns)
            surviving = []
            for line in changes.get("added", []):
                stripped = line.strip()
                s = score_line(stripped)
                if s <= 0:
                    continue
                if stripped in docker_content and stripped not in base_content:
                    surviving.append((s + prio, stripped, "positive"))

            # Check removed lines (negative patterns)
            for line in changes.get("removed", []):
                stripped = line.strip()
                s = score_line(stripped)
                if s <= 0:
                    continue
                if stripped in base_content and stripped not in docker_content:
                    surviving.append((s + prio + 50, stripped, "negative"))

            if surviving:
                surviving.sort(key=lambda x: -x[0])
                candidates.append((surviving[0][0], fpath, surviving))

        if not candidates:
            results[pr] = {"status": "no_patterns"}
            print(f"  PR {pr}: no surviving patterns")
            continue

        candidates.sort(key=lambda x: -x[0])
        best_file = candidates[0][1]
        best_patterns = candidates[0][2]

        seen = set()
        unique = []
        for _, pattern, ptype in best_patterns:
            stem = pattern[:30]
            if stem in seen:
                continue
            seen.add(stem)
            unique.append({"pattern": pattern, "type": ptype})
            if len(unique) >= 3:
                break

        results[pr] = {
            "status": "found",
            "file": best_file,
            "patterns": unique,
        }
        types = [p["type"] for p in unique]
        print(f"  PR {pr}: {best_file} ({len(unique)} patterns, types={types})")
        for p in unique:
            print(f"    {p['type']}: {p['pattern'][:60]}")

    # Output JSON
    output_path = f"/tmp/docker_patterns_{task_name}.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to {output_path}")

    found = sum(1 for r in results.values() if r.get("status") == "found")
    print(f"Found patterns: {found}/{len(prs)}")


if __name__ == "__main__":
    main()
