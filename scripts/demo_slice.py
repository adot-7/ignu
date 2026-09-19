#!/usr/bin/env python3
"""Replay a small, paced cohort through the cached pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import ingest, pipeline
from app.config import get_settings
from app.mapping import load as load_mapping


def _parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=15, help="people to replay (default: %(default)s)")
    parser.add_argument("--slow", type=float, default=0.6, help="seconds between people")
    parser.add_argument("--ids", default="", help="comma-separated person ids to prefer")
    parser.add_argument("--file", type=Path, default=Path(settings.registrations_file))
    parser.add_argument("--mapping", default=settings.mapping_file)
    parser.add_argument("--event", default=settings.event_name)
    parser.add_argument("--prev", type=Path, default=None)
    return parser


def _ensure_people(file: Path, mapping: object, event: str) -> list:
    people = pipeline.list_people()
    if people or not file.exists():
        return people
    # A fresh laptop may not have run prerun yet.  Ingesting the source is
    # local-only and lets the selector find the planted sample rows; evidence
    # and profiles are still limited to the requested slice below.
    ingest.ingest_registrations(file, mapping, event, run_id="demo-prepare")
    return pipeline.list_people()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    mapping = load_mapping(args.mapping)
    people = _ensure_people(args.file, mapping, args.event)
    preferred = [item.strip() for item in args.ids.split(",") if item.strip()]
    selected = pipeline.select_demo_ids(people, args.n, preferred)
    if not selected:
        print("No persisted people found; run prerun or provide a valid source.", file=sys.stderr)
        return 1

    result = pipeline.run(
        args.file,
        mapping,
        args.event,
        person_ids=selected,
        slow=max(0.0, args.slow),
        prev_file=args.prev,
    )
    print(f"run_id={result.get('run_id')}")
    print(f"replayed={len(selected)}")
    print("disagreements")
    for row in list(result.get("disagreements") or []):
        print(json.dumps(row, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
