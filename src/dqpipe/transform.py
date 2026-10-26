"""Pure transformation of raw CSV rows into clean records plus a quarantine.

Every input row ends up in exactly one place: loaded, quarantined (with a reason) or dropped as an
older duplicate of the same order. ``TransformResult.accounted_for`` checks that invariant.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

STATUSES = frozenset({"placed", "paid", "shipped", "delivered", "cancelled", "refunded"})
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$")


@dataclass(frozen=True)
class Customer:
    customer_id: str
    email: str
    country: str


@dataclass(frozen=True)
class Order:
    order_id: str
    customer_id: str
    order_ts: datetime  # always UTC
    currency: str
    amount_minor: int  # in the order currency, e.g. cents
    usd_cents: int
    status: str


@dataclass(frozen=True)
class Rejected:
    source: str
    row_number: int  # 1-based data row, header excluded
    reason: str
    raw: str


@dataclass
class TransformResult:
    customers: list[Customer] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)
    duplicates_dropped: int = 0
    source_rows: dict[str, int] = field(default_factory=dict)

    def accounted_for(self) -> bool:
        loaded = {"customers": len(self.customers), "orders": len(self.orders)}
        for source, total in self.source_rows.items():
            rejected = sum(1 for r in self.rejected if r.source == source)
            dropped = self.duplicates_dropped if source == "orders" else 0
            if loaded[source] + rejected + dropped != total:
                return False
        return True


def _raw(row: Mapping[str, str | None]) -> str:
    return ",".join(f"{k}={v}" for k, v in row.items())


def parse_amount(text: str, exponent: int) -> int:
    """Parse a decimal string into minor units. Raises ValueError for anything not exact."""
    try:
        value = Decimal(text.strip())
    except InvalidOperation as e:
        raise ValueError("amount is not a decimal number") from e
    if not value.is_finite():
        raise ValueError("amount is not a decimal number")
    if value < 0:
        raise ValueError("amount is negative")
    minor = value.scaleb(exponent)
    if minor != minor.to_integral_value():
        raise ValueError("amount has more decimal places than the currency allows")
    return int(minor)


def parse_ts(text: str) -> datetime:
    """Parse an ISO-8601 timestamp that must carry a UTC offset, and convert it to UTC."""
    try:
        ts = datetime.fromisoformat(text.strip())
    except ValueError as e:
        raise ValueError("timestamp is not ISO-8601") from e
    if ts.tzinfo is None:
        raise ValueError("timestamp has no UTC offset")
    return ts.astimezone(UTC)


def to_usd_cents(amount_minor: int, exponent: int, rate_to_usd: Decimal) -> int:
    usd = (Decimal(amount_minor).scaleb(-exponent) * rate_to_usd * 100).quantize(
        Decimal(1), rounding=ROUND_HALF_EVEN
    )
    return int(usd)


def transform(
    customers: Iterable[Mapping[str, str | None]],
    orders: Iterable[Mapping[str, str | None]],
    fx: Mapping[str, tuple[Decimal, int]],
) -> TransformResult:
    """Clean customers and orders.

    ``fx`` maps currency code to (rate to USD, minor-unit exponent), e.g. ``{"JPY": (Decimal("0.0066"), 0)}``.
    """
    result = TransformResult()

    seen_emails: set[str] = set()
    customer_ids: set[str] = set()
    n = 0
    for n, row in enumerate(customers, start=1):
        cid = (row.get("customer_id") or "").strip()
        email = (row.get("email") or "").strip().lower()
        country = (row.get("country") or "").strip().upper()
        reason = None
        if not cid:
            reason = "missing customer_id"
        elif cid in customer_ids:
            reason = "duplicate customer_id"
        elif not EMAIL.match(email):
            reason = "invalid email"
        elif email in seen_emails:
            reason = "email already used by another customer"
        elif not re.fullmatch(r"[A-Z]{2}", country):
            reason = "country is not an ISO 3166 alpha-2 code"
        if reason:
            result.rejected.append(Rejected("customers", n, reason, _raw(row)))
            continue
        customer_ids.add(cid)
        seen_emails.add(email)
        result.customers.append(Customer(cid, email, country))
    result.source_rows["customers"] = n

    latest: dict[str, Order] = {}
    n = 0
    for n, row in enumerate(orders, start=1):
        try:
            oid = (row.get("order_id") or "").strip()
            if not oid:
                raise ValueError("missing order_id")
            cid = (row.get("customer_id") or "").strip()
            if cid not in customer_ids:
                raise ValueError("customer_id not found in customers")
            currency = (row.get("currency") or "").strip().upper()
            if currency not in fx:
                raise ValueError("no FX rate for currency")
            rate, exponent = fx[currency]
            status = (row.get("status") or "").strip().lower()
            if status not in STATUSES:
                raise ValueError("unknown status")
            ts = parse_ts(row.get("order_ts") or "")
            amount = parse_amount(row.get("amount") or "", exponent)
        except ValueError as e:
            result.rejected.append(Rejected("orders", n, str(e), _raw(row)))
            continue
        order = Order(oid, cid, ts, currency, amount, to_usd_cents(amount, exponent, rate), status)
        previous = latest.get(oid)
        if previous is not None:
            result.duplicates_dropped += 1
            if previous.order_ts >= order.order_ts:
                continue  # an older update of an order we already have
        latest[oid] = order
    result.source_rows["orders"] = n
    result.orders = sorted(latest.values(), key=lambda o: o.order_id)
    return result
