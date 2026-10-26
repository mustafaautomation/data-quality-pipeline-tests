from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from dqpipe.cli import main, read_csv, read_fx
from dqpipe.load import load
from dqpipe.transform import transform
from tests.conftest import RUN_AT, SAMPLE, generate

TABLES = ["customers", "orders", "quarantine", "load_audit", "fx_rates"]


def snapshot(con: Any) -> dict[str, list[tuple[object, ...]]]:
    # Cast to text: fetching TIMESTAMPTZ as Python objects would need pytz.
    return {t: sorted(con.execute(f"SELECT CAST(COLUMNS(*) AS VARCHAR) FROM {t}").fetchall()) for t in TABLES}


def test_reloading_the_same_run_is_idempotent(loaded_db: Any) -> None:
    before = snapshot(loaded_db)
    fx = read_fx(SAMPLE / "fx_rates.csv")
    result = transform(read_csv(SAMPLE / "customers.csv"), read_csv(SAMPLE / "orders.csv"), fx)
    load(loaded_db, result, fx, "r1", RUN_AT)
    assert snapshot(loaded_db) == before


def test_a_later_run_upserts_changed_orders(loaded_db: Any) -> None:
    fx = read_fx(SAMPLE / "fx_rates.csv")
    rows = read_csv(SAMPLE / "orders.csv")
    first = next(r for r in rows if r["status"] == "paid" and r["order_id"])
    changed = dict(first, status="refunded", order_ts="2026-10-25T23:00:00+00:00")
    result = transform(read_csv(SAMPLE / "customers.csv"), [changed], fx)
    load(loaded_db, result, fx, "r2", RUN_AT)
    statuses = loaded_db.execute(
        "SELECT status FROM orders WHERE order_id = ?", [first["order_id"]]
    ).fetchall()
    assert statuses == [("refunded",)]


def test_failed_load_rolls_back(loaded_db: Any) -> None:
    before = snapshot(loaded_db)
    fx = read_fx(SAMPLE / "fx_rates.csv")
    result = transform(read_csv(SAMPLE / "customers.csv"), read_csv(SAMPLE / "orders.csv"), fx)
    result.source_rows["unknown_source"] = 1  # makes the audit insert fail after the upserts
    with pytest.raises(KeyError):
        load(loaded_db, result, fx, "r9", RUN_AT)
    assert snapshot(loaded_db) == before


def test_cli_passes_on_the_sample(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    out = tmp_path / "result.json"
    db = tmp_path / "w.duckdb"
    code = main([str(SAMPLE), "--db", str(db), "--run-at", RUN_AT.isoformat(), "--json", str(out)])
    assert code == 0
    assert json.loads(out.read_text())["passed"] is True
    assert "Data-quality gate: PASS" in summary.read_text()
    with duckdb.connect(str(db)) as con:
        assert con.execute("SELECT count(*) FROM orders").fetchone() == (1982,)


def test_cli_fails_when_too_many_rows_are_quarantined(tmp_path: Path) -> None:
    broken = generate(tmp_path / "broken", "--bad-rate", "0.15", "--seed", "11")
    out = tmp_path / "result.json"
    assert main([str(broken), "--run-at", RUN_AT.isoformat(), "--json", str(out)]) == 1
    failed = [c["name"] for c in json.loads(out.read_text())["checks"] if c["violations"]]
    assert failed == ["run_quarantine_rate"]


@pytest.mark.parametrize(
    "args",
    [["missing-dir"], [str(SAMPLE), "--run-at", "2026-10-26T00:00:00"], [str(SAMPLE), "--run-at", "soon"]],
)
def test_cli_bad_input_exits_2(args: list[str]) -> None:
    assert main(args) == 2


def test_bad_fx_file_exits_2(tmp_path: Path) -> None:
    for name in ("customers.csv", "orders.csv"):
        (tmp_path / name).write_text((SAMPLE / name).read_text())
    (tmp_path / "fx_rates.csv").write_text("currency,rate_to_usd,exponent\nUSD,one,2\n")
    assert main([str(tmp_path)]) == 2
