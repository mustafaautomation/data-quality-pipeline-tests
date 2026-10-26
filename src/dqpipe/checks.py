"""Data-quality checks, each a SQL query that counts violating rows (0 = pass)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

Severity = Literal["error", "warn"]


@dataclass(frozen=True)
class Check:
    name: str
    severity: Severity
    description: str
    sql: str  # named parameters: $run_id, $run_at, $max_quarantine_pct, $freshness_hours, $volume_pct


@dataclass(frozen=True)
class Thresholds:
    max_quarantine_pct: float = 5.0
    freshness_hours: int = 48
    volume_change_pct: float = 50.0


@dataclass(frozen=True)
class CheckResult:
    check: Check
    violations: int

    @property
    def passed(self) -> bool:
        return self.violations == 0


CHECKS: tuple[Check, ...] = (
    Check(
        "orders_order_id_unique",
        "error",
        "No order_id appears more than once",
        "SELECT count(*) FROM (SELECT order_id FROM orders GROUP BY order_id HAVING count(*) > 1)",
    ),
    Check(
        "orders_required_fields",
        "error",
        "Every order has id, customer, timestamp, currency, amounts and status",
        """SELECT count(*) FROM orders WHERE order_id IS NULL OR customer_id IS NULL OR order_ts IS NULL
           OR currency IS NULL OR amount_minor IS NULL OR usd_cents IS NULL OR status IS NULL""",
    ),
    Check(
        "orders_status_accepted",
        "error",
        "Status is one of the known lifecycle values",
        """SELECT count(*) FROM orders
           WHERE status NOT IN ('placed', 'paid', 'shipped', 'delivered', 'cancelled', 'refunded')""",
    ),
    Check(
        "orders_amount_non_negative",
        "error",
        "Amounts are zero or positive",
        "SELECT count(*) FROM orders WHERE amount_minor < 0 OR usd_cents < 0",
    ),
    Check(
        "orders_customer_exists",
        "error",
        "Every order's customer exists (referential integrity)",
        """SELECT count(*) FROM orders o LEFT JOIN customers c USING (customer_id)
           WHERE c.customer_id IS NULL""",
    ),
    Check(
        "orders_usd_matches_fx",
        "error",
        "usd_cents equals amount converted at the stored FX rate (within 1 cent)",
        """SELECT count(*) FROM orders o LEFT JOIN fx_rates f USING (currency)
           WHERE f.currency IS NULL
              OR abs(o.usd_cents - round(o.amount_minor / pow(10, f.exponent) * f.rate_to_usd * 100)) > 1""",
    ),
    Check(
        "orders_not_in_future",
        "error",
        "No order is timestamped more than 5 minutes after the run",
        "SELECT count(*) FROM orders WHERE order_ts > CAST($run_at AS TIMESTAMPTZ) + INTERVAL 5 MINUTE",
    ),
    Check(
        "customers_id_unique",
        "error",
        "No customer_id appears more than once",
        "SELECT count(*) FROM (SELECT customer_id FROM customers GROUP BY 1 HAVING count(*) > 1)",
    ),
    Check(
        "customers_email_unique",
        "error",
        "No email belongs to two customers",
        "SELECT count(*) FROM (SELECT email FROM customers GROUP BY 1 HAVING count(*) > 1)",
    ),
    Check(
        "run_rows_reconcile",
        "error",
        "For this run, source rows = loaded + quarantined + duplicates dropped, per source",
        """SELECT count(*) FROM load_audit WHERE run_id = $run_id
           AND source_rows <> loaded + quarantined + duplicates_dropped""",
    ),
    Check(
        "run_quarantine_rate",
        "error",
        "For this run, no source quarantines more than the allowed share of its rows",
        """SELECT count(*) FROM load_audit WHERE run_id = $run_id AND source_rows > 0
           AND 100.0 * quarantined / source_rows > $max_quarantine_pct""",
    ),
    Check(
        "orders_fresh",
        "warn",
        "The newest order is no older than the freshness window",
        """SELECT CASE WHEN max(order_ts) IS NULL
                         OR max(order_ts) < CAST($run_at AS TIMESTAMPTZ)
                                            - to_hours(CAST($freshness_hours AS BIGINT))
                       THEN 1 ELSE 0 END FROM orders""",
    ),
    Check(
        "run_volume_stable",
        "warn",
        "Orders loaded in this run are within the allowed change from the previous runs' average",
        """WITH this AS (SELECT loaded FROM load_audit WHERE run_id = $run_id AND source = 'orders'),
                prev AS (SELECT avg(loaded) AS avg_loaded FROM load_audit
                         WHERE source = 'orders' AND run_id <> $run_id
                           AND run_at < (SELECT max(run_at) FROM load_audit WHERE run_id = $run_id))
           SELECT count(*) FROM this, prev
           WHERE prev.avg_loaded > 0
             AND abs(this.loaded - prev.avg_loaded) * 100.0 / prev.avg_loaded > $volume_pct""",
    ),
)


def run_checks(
    con: Any, run_id: str, run_at: datetime, thresholds: Thresholds | None = None
) -> list[CheckResult]:
    t = thresholds or Thresholds()
    params = {
        "run_id": run_id,
        "run_at": run_at,
        "max_quarantine_pct": t.max_quarantine_pct,
        "freshness_hours": t.freshness_hours,
        "volume_pct": t.volume_change_pct,
    }
    results = []
    for check in CHECKS:
        used = {k: v for k, v in params.items() if f"${k}" in check.sql}
        row = con.execute(check.sql, used).fetchone()
        results.append(CheckResult(check, int(row[0]) if row else 0))
    return results


def gate_passes(results: list[CheckResult]) -> bool:
    return all(r.passed for r in results if r.check.severity == "error")
