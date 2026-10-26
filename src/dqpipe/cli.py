"""dqpipe: run the orders ETL into DuckDB, then the data-quality checks that gate it.

Exit codes: 0 all error-level checks passed, 1 at least one failed, 2 bad input.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import duckdb

from dqpipe.checks import CheckResult, Thresholds, gate_passes, run_checks
from dqpipe.load import load
from dqpipe.transform import transform


class InputError(Exception):
    pass


def read_csv(path: Path) -> list[dict[str, str | None]]:
    if not path.is_file():
        raise InputError(f"missing input file {path}")
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_fx(path: Path) -> dict[str, tuple[Decimal, int]]:
    fx: dict[str, tuple[Decimal, int]] = {}
    for row in read_csv(path):
        try:
            fx[(row["currency"] or "").upper()] = (
                Decimal(row["rate_to_usd"] or ""),
                int(row["exponent"] or ""),
            )
        except (InvalidOperation, ValueError, KeyError) as e:
            raise InputError(f"bad FX row {row}: {e}") from e
    return fx


def markdown(results: list[CheckResult], run_id: str, audit: list[tuple[object, ...]]) -> str:
    ok = gate_passes(results)
    lines = [
        f"## Data-quality gate: {'PASS' if ok else 'FAIL'}",
        "",
        f"Run `{run_id}`",
        "",
        "| Source | Rows | Loaded | Quarantined | Duplicates dropped |",
        "|---|---|---|---|---|",
        *(f"| {s} | {r} | {lo} | {q} | {d} |" for s, r, lo, q, d in audit),
        "",
        "| Check | Severity | Violations | Result |",
        "|---|---|---|---|",
    ]
    for r in results:
        status = "pass" if r.passed else ("FAIL" if r.check.severity == "error" else "warn")
        lines.append(f"| {r.check.name} | {r.check.severity} | {r.violations} | {status} |")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dqpipe", description=__doc__)
    p.add_argument("input", type=Path, help="directory with customers.csv, orders.csv, fx_rates.csv")
    p.add_argument("--db", default=":memory:", help="DuckDB file (default: in memory)")
    p.add_argument("--run-id", default=None)
    p.add_argument("--run-at", default=None, help="ISO timestamp for the run (default: now, UTC)")
    p.add_argument("--max-quarantine-pct", type=float, default=Thresholds.max_quarantine_pct)
    p.add_argument("--json", type=Path, default=None, help="also write results as JSON here")
    args = p.parse_args(argv)

    try:
        run_at = datetime.fromisoformat(args.run_at) if args.run_at else datetime.now(UTC)
        if run_at.tzinfo is None:
            raise InputError("--run-at needs a UTC offset")
        run_id = args.run_id or run_at.strftime("%Y%m%dT%H%M%SZ")
        fx = read_fx(args.input / "fx_rates.csv")
        result = transform(read_csv(args.input / "customers.csv"), read_csv(args.input / "orders.csv"), fx)
    except (InputError, ValueError) as e:
        print(f"dqpipe: {e}", file=sys.stderr)
        return 2

    con = duckdb.connect(args.db)
    try:
        load(con, result, fx, run_id, run_at)
        results = run_checks(con, run_id, run_at, Thresholds(max_quarantine_pct=args.max_quarantine_pct))
        audit = con.execute(
            "SELECT source, source_rows, loaded, quarantined, duplicates_dropped FROM load_audit "
            "WHERE run_id = ? ORDER BY source",
            [run_id],
        ).fetchall()
        reasons = con.execute(
            "SELECT source, reason, count(*) FROM quarantine WHERE run_id = ? "
            "GROUP BY ALL ORDER BY 3 DESC, 2 LIMIT 10",
            [run_id],
        ).fetchall()
    finally:
        con.close()

    report = markdown(results, run_id, audit)
    if reasons:
        report += (
            "\nTop quarantine reasons:\n\n" + "\n".join(f"- {s}: {r} ({n})" for s, r, n in reasons) + "\n"
        )
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report)
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "passed": gate_passes(results),
                    "checks": [
                        {"name": r.check.name, "severity": r.check.severity, "violations": r.violations}
                        for r in results
                    ],
                },
                indent=2,
            )
        )
    return 0 if gate_passes(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
