#!/usr/bin/env python3
"""Legacy portfolio helper with missing factor loadings."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


REFUSED = "FINANCE-CASE-REFUSED: "


def compact(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario")
    parser.add_argument("--journal")
    parser.add_argument("--report")
    parser.add_argument("--inspect", action="store_true")
    args = parser.parse_args()
    if args.inspect:
        return 0
    if not args.scenario or not args.journal or not args.report:
        print(REFUSED + "scenario journal and report are required", file=sys.stderr)
        return 3
    try:
        scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
        asset_ids = [item["id"] for item in scenario["assets"]]
        factor_ids = list(scenario["factors"])
        base = {
            "allocations": {asset: 0 for asset in asset_ids},
            "case_id": scenario["case_id"],
            "exposure": {factor: 0.0 for factor in factor_ids},
            "factor_loadings": {
                asset: {factor: 0.0 for factor in factor_ids} for asset in asset_ids
            },
            "objective": 0.0,
            "schema_version": 1,
            "solver_nodes": 0,
            "spend": 0.0,
        }
        checksum = hashlib.sha256(compact(base)).hexdigest()
        entry = {
            "case_id": scenario["case_id"],
            "input_sha256": hashlib.sha256(compact(scenario)).hexdigest(),
            "report_sha256": hashlib.sha256(compact({**base, "checksum": checksum})).hexdigest(),
            "status": "completed",
        }
        report = {**base, "checksum": checksum, "journal_entry": entry}
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_bytes(compact(report))
        journal_path = Path(args.journal)
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        with journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
        return 0
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(REFUSED + str(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
