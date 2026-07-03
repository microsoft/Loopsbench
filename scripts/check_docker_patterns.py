#!/usr/bin/env python3
"""Check which patterns from gold patches survive in the current workspace."""
import os
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

prs = [15208, 15382, 15410]
for pr in prs:
    diff_path = f"gold_patches/{pr}.diff"
    if not os.path.exists(diff_path):
        print(f"PR {pr}: no diff")
        continue
    diff = parse_diff(diff_path)
    print(f"=== PR {pr} ===")
    for fpath, changes in diff.items():
        if not os.path.exists(fpath):
            continue
        with open(fpath, errors="replace") as f:
            final = f.read()
        surviving = []
        for line in changes["added"]:
            stripped = line.strip()
            if len(stripped) >= 8 and stripped in final:
                surviving.append(stripped)
        if surviving:
            print(f"  {fpath}: {len(surviving)} surviving")
            for s in surviving[:5]:
                print(f"    + {s[:100]}")
        else:
            print(f"  {fpath}: 0 surviving")
