"""Generate a deterministic synthetic orders dataset.

uv run python scripts/generate.py data/sample                    # clean-ish: ~1% bad rows
uv run python scripts/generate.py out/broken --bad-rate 0.15     # enough bad rows to trip the gate
"""

from __future__ import annotations

import argparse
import csv
import random
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

ANCHOR = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
FX = [("USD", "1", 2), ("EUR", "1.08", 2), ("GBP", "1.27", 2), ("MYR", "0.2125", 2), ("JPY", "0.0066", 0)]
STATUSES = ["placed", "paid", "shipped", "delivered", "cancelled", "refunded"]
COUNTRIES = ["MY", "SG", "GB", "DE", "JP", "US", "AE"]


def bad_order(rng: random.Random, row: dict[str, str]) -> dict[str, str]:
    kind = rng.choice(["amount", "negative", "currency", "status", "ts", "customer", "id", "naive_ts"])
    if kind == "amount":
        row["amount"] = "12,50"
    elif kind == "negative":
        row["amount"] = "-5.00"
    elif kind == "currency":
        row["currency"] = "XXX"
    elif kind == "status":
        row["status"] = "lost"
    elif kind == "ts":
        row["order_ts"] = "25/10/2026 10:00"
    elif kind == "customer":
        row["customer_id"] = "C99999"
    elif kind == "id":
        row["order_id"] = ""
    else:
        row["order_ts"] = row["order_ts"][:19]
    return row


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("out", type=Path)
    p.add_argument("--customers", type=int, default=200)
    p.add_argument("--orders", type=int, default=2000)
    p.add_argument("--bad-rate", type=float, default=0.01)
    p.add_argument("--dup-rate", type=float, default=0.03)
    p.add_argument("--seed", type=int, default=7)
    a = p.parse_args()
    rng = random.Random(a.seed)
    a.out.mkdir(parents=True, exist_ok=True)

    with (a.out / "fx_rates.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["currency", "rate_to_usd", "exponent"])
        w.writerows(FX)

    with (a.out / "customers.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["customer_id", "email", "country"])
        for i in range(1, a.customers + 1):
            w.writerow([f"C{i:05d}", f"  User{i}@Example.com ", rng.choice(COUNTRIES).lower()])

    rows: list[dict[str, str]] = []
    for i in range(1, a.orders + 1):
        cur, _, exp = rng.choice(FX)
        amount = rng.randint(100, 50000)
        offset = timezone(timedelta(hours=rng.choice([-5, 0, 1, 8, 9])))
        ts = (ANCHOR - timedelta(minutes=rng.randint(0, 60 * 24 * 7))).astimezone(offset)
        row = {
            "order_id": f"O{i:07d}",
            "customer_id": f"C{rng.randint(1, a.customers):05d}",
            "order_ts": ts.isoformat(timespec="seconds"),
            "currency": cur,
            "amount": str(amount) if exp == 0 else f"{amount // 100}.{amount % 100:02d}",
            "status": rng.choice(STATUSES),
        }
        rows.append(bad_order(rng, row) if rng.random() < a.bad_rate else row)
        if rng.random() < a.dup_rate:  # a later status update for the same order
            update = dict(row)
            update["status"] = "delivered"
            update["order_ts"] = (datetime.fromisoformat(row["order_ts"]) + timedelta(hours=6)).isoformat()
            rows.append(update)

    with (a.out / "orders.csv").open("w", newline="") as f:
        dw = csv.DictWriter(f, fieldnames=list(rows[0]))
        dw.writeheader()
        dw.writerows(rows)


if __name__ == "__main__":
    main()
