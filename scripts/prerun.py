#!/usr/bin/env python3
"""Warm the complete registration/evidence/profile cache for the demo."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import llm, pipeline
from app.config import get_settings


def _parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="registrations CSV/XLSX to ingest")
    parser.add_argument(
        "--mapping",
        default=settings.mapping_file,
        help="mapping YAML (default: %(default)s)",
    )
    parser.add_argument(
        "--event",
        default=settings.event_name,
        help="event name (default: %(default)s)",
    )
    parser.add_argument(
        "--prev",
        type=Path,
        default=None,
        help="optional previous-event CSV/XLSX",
    )
    return parser


def _serialise_report(report: object) -> dict:
    if hasattr(report, "__dataclass_fields__"):
        return asdict(report)  # type: ignore[arg-type]
    if hasattr(report, "model_dump"):
        return report.model_dump()  # type: ignore[no-any-return]
    return {}


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = pipeline.run(
        args.file,
        args.mapping,
        args.event,
        prev_file=args.prev,
    )
    report = _serialise_report(result.get("ingest"))
    spend = float(llm.spend_usd())
    budget = float(get_settings().llm_budget_usd)

    print("IngestReport")
    print(json.dumps(report, sort_keys=True))
    print(f"run_id={result.get('run_id')}")
    print(f"LLM spend=${spend:.6f} / ${budget:.6f}")
    print("Top disagreements")
    for row in list(result.get("disagreements") or [])[:10]:
        print(json.dumps(row, sort_keys=True, default=str))

    if spend > budget * 0.6:
        print(
            f"WARNING: spend ${spend:.6f} exceeds 60% of the configured budget; "
            "pause before the live demo.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
