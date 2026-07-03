#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, UTC
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from long_horizon_bench.task_images.docker_ops import publish_task_image
from long_horizon_bench.task_images.registry import load_manifest, write_manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build, push, and verify published non-seg task images."
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
    parser.add_argument(
        "--tasks-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "tasks",
    )
    parser.add_argument(
        "--task-id",
        action="append",
        dest="task_ids",
        default=[],
        help="Limit publication to one or more task ids.",
    )
    parser.add_argument(
        "--alias-tag",
        action="append",
        dest="alias_tags",
        default=[],
        help="Additional tags to publish after the primary image tag.",
    )
    parser.add_argument(
        "--notes",
        default="",
        help="Optional note recorded into the manifest for each published task.",
    )
    return parser


def _compose_path_for_record(tasks_root: Path, compose_file: str) -> Path:
    return (tasks_root.parent / compose_file).resolve()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    records = load_manifest(args.manifest)
    wanted = set(args.task_ids)
    updated = []
    for record in records:
        if wanted and record.task_id not in wanted:
            updated.append(record)
            continue
        digest = publish_task_image(
            compose_file=_compose_path_for_record(args.tasks_root, record.compose_file),
            image_ref=record.image_ref,
            platform=record.platform,
            alias_tags=args.alias_tags,
        )
        updated.append(
            record.model_copy(
                update={
                    "image_digest": digest,
                    "published_at": datetime.now(UTC)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "verified": True,
                    "notes": args.notes,
                }
            )
        )
    write_manifest(args.manifest, updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
