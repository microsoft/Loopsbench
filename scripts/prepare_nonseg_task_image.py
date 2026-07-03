#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from long_horizon_bench.task_images.docker_ops import prepare_local_task_image
from long_horizon_bench.task_images.registry import (
    load_manifest,
    local_client_image_name,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pull a published non-seg task image and retag it for local harness use."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=(
            Path(__file__).resolve().parents[1]
            / "metadata"
            / "docker_images"
            / "nonseg.yaml"
        ),
    )
    parser.add_argument("--task-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for record in load_manifest(args.manifest):
        if record.task_id == args.task_id:
            prepare_local_task_image(
                image_ref=record.image_ref,
                local_image_name=local_client_image_name(record.task_id),
            )
            return 0
    raise SystemExit(f"task_id not found in manifest: {args.task_id}")


if __name__ == "__main__":
    raise SystemExit(main())
