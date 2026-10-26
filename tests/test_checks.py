from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from dqpipe.checks import CHECKS, gate_passes, run_checks
from dqpipe.cli import read_csv, read_fx
from dqpipe.load import load
from dqpipe.transform import transform
from tests.conftest import RUN_AT, SAMPLE

FIRST = "(SELECT min(order_id) FROM orders)"

# Each defect is planted straight into the tables (as a buggy upstream job would) and must trip
# exactly the listed checks and nothing else.
DEFECTS = [
    ("duplicate order", "INSERT INTO orders SELECT * FROM orders LIMIT 1", {"orders_order_id_unique"}),
    ("null status", f"UPDATE orders SET status = NULL WHERE order_id = {FIRST}", {"orders_required_fields"}),
    (
        "unknown status",
        f"UPDATE orders SET status = 'lost' WHERE order_id = {FIRST}",
        {"orders_status_accepted"},
    ),
    (
        "negative amount",
        f"UPDATE orders SET amount_minor = -amount_minor, usd_cents = -usd_cents WHERE order_id = {FIRST}",
        {"orders_amount_non_negative"},
    ),
    (
        "orphan order",
        f"UPDATE orders SET customer_id = 'C404' WHERE order_id = {FIRST}",
        {"orders_customer_exists"},
    ),
    (
        "wrong USD",
        f"UPDATE orders SET usd_cents = usd_cents + 500 WHERE order_id = {FIRST}",
        {"orders_usd_matches_fx"},
    ),
    ("missing FX rate", "DELETE FROM fx_rates WHERE currency = 'JPY'", {"orders_usd_matches_fx"}),
    (
        "future order",
        f"UPDATE orders SET order_ts = TIMESTAMPTZ '2026-10-27 00:00:00+00' WHERE order_id = {FIRST}",
        {"orders_not_in_future"},
    ),
    (
        "duplicate customer",
        "INSERT INTO customers SELECT * FROM customers LIMIT 1",
        {"customers_id_unique", "customers_email_unique"},
    ),
    (
        "shared email",
        "UPDATE customers SET email = 'user1@example.com' WHERE customer_id = 'C00002'",
        {"customers_email_unique"},
    ),
    (
        "audit mismatch",
        "UPDATE load_audit SET loaded = loaded - 1 WHERE source = 'orders'",
        {"run_rows_reconcile"},
    ),
    (
        "quarantine spike",
        "UPDATE load_audit SET quarantined = quarantined + 200, loaded = loaded - 200 "
        "WHERE source = 'orders'",
        {"run_quarantine_rate"},
    ),
]


def failing(con: Any, run_at: Any = RUN_AT) -> set[str]:
    return {r.check.name for r in run_checks(con, "r1", run_at) if not r.passed}


def test_clean_sample_passes_every_check(loaded_db: Any) -> None:
    results = run_checks(loaded_db, "r1", RUN_AT)
    assert [r.check.name for r in results if not r.passed] == []
    assert gate_passes(results)


@pytest.mark.parametrize(("name", "sql", "expected"), DEFECTS, ids=[d[0] for d in DEFECTS])
def test_each_defect_trips_exactly_its_checks(
    loaded_db: Any, name: str, sql: str, expected: set[str]
) -> None:
    loaded_db.execute(sql)
    assert failing(loaded_db) == expected
    assert not gate_passes(run_checks(loaded_db, "r1", RUN_AT))


def test_every_error_check_has_a_defect_test() -> None:
    covered = set().union(*(d[2] for d in DEFECTS))
    assert {c.name for c in CHECKS if c.severity == "error"} == covered


def test_stale_data_only_warns(loaded_db: Any) -> None:
    later = RUN_AT + timedelta(days=10)
    results = run_checks(loaded_db, "r1", later)
    assert {r.check.name for r in results if not r.passed} == {"orders_fresh"}
    assert gate_passes(results)


def test_volume_drop_against_previous_runs_warns(loaded_db: Any) -> None:
    fx = read_fx(SAMPLE / "fx_rates.csv")
    orders = read_csv(SAMPLE / "orders.csv")[:300]
    result = transform(read_csv(SAMPLE / "customers.csv"), orders, fx)
    load(loaded_db, result, fx, "r2", RUN_AT + timedelta(hours=1))
    results = run_checks(loaded_db, "r2", RUN_AT + timedelta(hours=1))
    assert {r.check.name for r in results if not r.passed} == {"run_volume_stable"}
    assert gate_passes(results)


def test_empty_orders_table_is_stale(loaded_db: Any) -> None:
    loaded_db.execute("DELETE FROM orders")
    assert "orders_fresh" in failing(loaded_db)
