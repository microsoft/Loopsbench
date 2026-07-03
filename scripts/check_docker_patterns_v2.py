#!/usr/bin/env python3
"""Check which patterns from gold patches survive in the current workspace.
Works for any repo type. Checks both Docker final state and base absence."""
import os
import re
from collections import defaultdict

def parse_diff(p):
    result = defaultdict(lambda: {"added": [], "removed": []})
    cur = None
    with open(p, errors="replace") as f:
        for line in f:
            if line.startswith("+++ b/"):
                cur = line[6:].strip()
            elif cur and line.startswith("+") and not line.startswith("+++"):
                result[cur]["added"].append(line[1:].rstrip())
    return dict(result)

prs = os.environ.get("PRS", "").split(",")
base_dir = os.environ.get("BASE_DIR", "")

for pr in prs:
    pr = pr.strip()
    if not pr:
        continue
    diff_path = f"gold_patches/{pr}.diff"
    if not os.path.exists(diff_path):
        print(f"PR {pr}: no diff")
        continue
    diff = parse_diff(diff_path)
    print(f"=== PR {pr} ===")

    found_any = False
    for fpath, changes in diff.items():
        if "/test/" in fpath or fpath.startswith("test/"):
            continue
        if fpath.endswith((".json", ".md", ".yml", ".lock", ".txt", ".map")):
            continue
        if not os.path.exists(fpath):
            continue

        with open(fpath, errors="replace") as f:
            final = f.read()

        for line in changes["added"]:
            stripped = line.strip()
            if len(stripped) < 10:
                continue
            if stripped in final:
                # Check base (if available via env)
                if base_dir:
                    base_file = os.path.join(base_dir, fpath)
                    if os.path.exists(base_file):
                        with open(base_file, errors="replace") as bf:
                            base_content = bf.read()
                        if stripped in base_content:
                            continue

                print(f"  {fpath}: {stripped[:100]}")
                found_any = True

    if not found_any:
        print(f"  NO surviving unique patterns")
