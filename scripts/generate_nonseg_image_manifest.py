#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from long_horizon_bench.task_images.registry import (
    build_manifest_records,
    write_manifest,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate the non-seg task image manifest."
    )
    parser.add_argument(
        "--tasks-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "tasks",
    )
    parser.add_argument(
        "--manifest",
        "--output",
        type=Path,
        default=(
            Path(__file__).resolve().parents[1]
            / "metadata"
            / "docker_images"
            / "nonseg.yaml"
        ),
    )
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--source-ref", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    records = build_manifest_records(
        tasks_root=args.tasks_root,
        namespace=args.namespace,
        tag=args.tag,
        platform=args.platform,
        source_ref=args.source_ref,
    )
    write_manifest(args.manifest, records)
    print(f"wrote {len(records)} image records to {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
