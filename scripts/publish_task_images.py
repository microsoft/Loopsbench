#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from long_horizon_bench.task_images.docker_ops import publish_task_image
from long_horizon_bench.task_images.registry import discover_task_images
from long_horizon_bench.task_images.strategy import remote_client_image_ref


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and publish task images directly from the tasks tree."
    )
    parser.add_argument(
        "--tasks-root",
        type=Path,
        default=REPO_ROOT / "tasks",
    )
    parser.add_argument(
        "--task-id",
        action="append",
        dest="task_ids",
        default=[],
        help="Limit publication to one or more task ids.",
    )
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument(
        "--alias-tag",
        action="append",
        dest="alias_tags",
        default=[],
        help="Additional tags to publish after the primary image tag.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    wanted = set(args.task_ids)
    for source in discover_task_images(args.tasks_root):
        if wanted and source.task_id not in wanted:
            continue
        image_ref = remote_client_image_ref(args.namespace, source.task_id, args.tag)
        publish_task_image(
            compose_file=source.compose_file,
            image_ref=image_ref,
            platform=args.platform,
            alias_tags=args.alias_tags,
        )
        print(f"published {source.task_id} -> {image_ref}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
