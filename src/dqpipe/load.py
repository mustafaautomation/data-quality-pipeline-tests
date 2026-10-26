"""Load a TransformResult into DuckDB.

The tables deliberately have no PRIMARY KEY or FOREIGN KEY constraints, like most cloud warehouses
(BigQuery, Snowflake and Redshift do not enforce them). Uniqueness and referential integrity are
therefore the data-quality checks' job, which is the point of this project.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from dqpipe.transform import TransformResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (customer_id VARCHAR, email VARCHAR, country VARCHAR);
CREATE TABLE IF NOT EXISTS orders (
    order_id VARCHAR, customer_id VARCHAR, order_ts TIMESTAMPTZ, currency VARCHAR,
    amount_minor BIGINT, usd_cents BIGINT, status VARCHAR
);
CREATE TABLE IF NOT EXISTS fx_rates (currency VARCHAR, rate_to_usd DECIMAL(18, 8), exponent INTEGER);
CREATE TABLE IF NOT EXISTS quarantine (
    run_id VARCHAR, source VARCHAR, row_number INTEGER, reason VARCHAR, raw VARCHAR
);
CREATE TABLE IF NOT EXISTS load_audit (
    run_id VARCHAR, run_at TIMESTAMPTZ, source VARCHAR, source_rows INTEGER, loaded INTEGER,
    quarantined INTEGER, duplicates_dropped INTEGER
);
"""


def create_schema(con: Any) -> None:
    con.execute(SCHEMA)


def load(
    con: Any,
    result: TransformResult,
    fx: Mapping[str, tuple[Decimal, int]],
    run_id: str,
    run_at: datetime,
) -> None:
    """Upsert customers and orders by id and record the run. Re-running the same run is idempotent."""
    create_schema(con)
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute("DELETE FROM fx_rates")
        con.executemany(
            "INSERT INTO fx_rates VALUES (?, ?, ?)", [(c, r, e) for c, (r, e) in sorted(fx.items())]
        )
        if result.customers:
            con.executemany(
                "DELETE FROM customers WHERE customer_id = ?", [(c.customer_id,) for c in result.customers]
            )
            con.executemany(
                "INSERT INTO customers VALUES (?, ?, ?)",
                [(c.customer_id, c.email, c.country) for c in result.customers],
            )
        if result.orders:
            con.executemany("DELETE FROM orders WHERE order_id = ?", [(o.order_id,) for o in result.orders])
            con.executemany(
                "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (o.order_id, o.customer_id, o.order_ts, o.currency, o.amount_minor, o.usd_cents, o.status)
                    for o in result.orders
                ],
            )
        con.execute("DELETE FROM quarantine WHERE run_id = ?", [run_id])
        if result.rejected:
            con.executemany(
                "INSERT INTO quarantine VALUES (?, ?, ?, ?, ?)",
                [(run_id, r.source, r.row_number, r.reason, r.raw) for r in result.rejected],
            )
        con.execute("DELETE FROM load_audit WHERE run_id = ?", [run_id])
        loaded = {"customers": len(result.customers), "orders": len(result.orders)}
        for source, total in sorted(result.source_rows.items()):
            con.execute(
                "INSERT INTO load_audit VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    run_id,
                    run_at,
                    source,
                    total,
                    loaded[source],
                    sum(1 for r in result.rejected if r.source == source),
                    result.duplicates_dropped if source == "orders" else 0,
                ],
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
