#!/usr/bin/env python3
"""Restore the SQLite state captured by the latest full prerun."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pipeline
from app.config import get_settings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", default=get_settings().event_name)
    args = parser.parse_args(argv)
    result = pipeline.reset_demo(args.event)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") in {"ok", "skip"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
