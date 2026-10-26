from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dqpipe.transform import parse_amount, parse_ts, to_usd_cents, transform
from tests.conftest import FX

CUSTOMERS = [{"customer_id": "C1", "email": "a@example.com", "country": "my"}]


def order(**kw: str) -> dict[str, str | None]:
    row: dict[str, str | None] = {
        "order_id": "O1",
        "customer_id": "C1",
        "order_ts": "2026-10-25T10:00:00+08:00",
        "currency": "USD",
        "amount": "10.50",
        "status": "paid",
    }
    row.update(kw)
    return row


@pytest.mark.parametrize(
    ("text", "exponent", "minor"),
    [("10.50", 2, 1050), ("0", 2, 0), ("1200", 0, 1200), (" 3.1 ", 2, 310), ("1e2", 2, 10000)],
)
def test_parse_amount(text: str, exponent: int, minor: int) -> None:
    assert parse_amount(text, exponent) == minor


@pytest.mark.parametrize(
    ("text", "exponent", "reason"),
    [
        ("12,50", 2, "not a decimal"),
        ("NaN", 2, "not a decimal"),
        ("Infinity", 2, "not a decimal"),
        ("-1.00", 2, "negative"),
        ("1.005", 2, "decimal places"),
        ("10.5", 0, "decimal places"),
    ],
)
def test_parse_amount_rejects(text: str, exponent: int, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        parse_amount(text, exponent)


def test_parse_ts_converts_to_utc_and_needs_an_offset() -> None:
    assert parse_ts("2026-10-25T10:00:00+08:00") == datetime(2026, 10, 25, 2, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="no UTC offset"):
        parse_ts("2026-10-25T10:00:00")
    with pytest.raises(ValueError, match="ISO-8601"):
        parse_ts("25/10/2026")


def test_usd_conversion_rounds_half_to_even() -> None:
    assert to_usd_cents(1, 2, Decimal("0.5")) == 0  # 0.5 cent -> 0
    assert to_usd_cents(3, 2, Decimal("0.5")) == 2  # 1.5 cents -> 2
    assert to_usd_cents(1000, 0, Decimal("0.0066")) == 660  # JPY has no minor unit


def test_customer_rules() -> None:
    rows: list[dict[str, str | None]] = [
        {"customer_id": "C1", "email": " A@Example.COM ", "country": "my"},
        {"customer_id": "C1", "email": "b@example.com", "country": "MY"},
        {"customer_id": "C2", "email": "a@example.com", "country": "MY"},
        {"customer_id": "C3", "email": "not-an-email", "country": "MY"},
        {"customer_id": "C4", "email": "d@example.com", "country": "Malaysia"},
        {"customer_id": "", "email": "e@example.com", "country": "MY"},
    ]
    r = transform(rows, [], FX)
    assert [(c.customer_id, c.email, c.country) for c in r.customers] == [("C1", "a@example.com", "MY")]
    assert [x.reason for x in r.rejected] == [
        "duplicate customer_id",
        "email already used by another customer",
        "invalid email",
        "country is not an ISO 3166 alpha-2 code",
        "missing customer_id",
    ]
    assert r.accounted_for()


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"order_id": " "}, "missing order_id"),
        ({"customer_id": "C9"}, "customer_id not found"),
        ({"currency": "XXX"}, "no FX rate"),
        ({"status": "lost"}, "unknown status"),
        ({"order_ts": "2026-10-25 10:00"}, "no UTC offset"),
        ({"amount": "-1"}, "negative"),
        ({"amount": None}, "not a decimal"),
    ],
)
def test_order_rejections(override: dict[str, str | None], reason: str) -> None:
    r = transform(CUSTOMERS, [order(**override)], FX)  # type: ignore[arg-type]
    assert r.orders == []
    assert len(r.rejected) == 1 and reason in r.rejected[0].reason
    assert r.rejected[0].row_number == 1 and r.accounted_for()


def test_latest_update_wins_regardless_of_file_order() -> None:
    newer = order(status="delivered", order_ts="2026-10-25T12:00:00+00:00")
    older = order(status="paid", order_ts="2026-10-25T09:00:00+00:00")
    for rows in ([older, newer], [newer, older]):
        r = transform(CUSTOMERS, rows, FX)
        assert [o.status for o in r.orders] == ["delivered"]
        assert r.duplicates_dropped == 1 and r.accounted_for()


def test_normalises_fields() -> None:
    r = transform(CUSTOMERS, [order(currency=" eur ", status="PAID", amount="10.00")], FX)
    (o,) = r.orders
    assert (o.currency, o.status, o.amount_minor, o.usd_cents) == ("EUR", "paid", 1000, 1080)
    assert o.order_ts.tzinfo == UTC


valid_or_junk = st.one_of(st.none(), st.text(max_size=12))
rows_strategy = st.lists(
    st.fixed_dictionaries(
        {
            "order_id": st.sampled_from(["O1", "O2", "O3", "", None]),
            "customer_id": st.sampled_from(["C1", "C2", None]),
            "order_ts": st.one_of(
                st.datetimes(timezones=st.just(UTC)).map(lambda d: d.isoformat()), valid_or_junk
            ),
            "currency": st.sampled_from(["USD", "eur", "JPY", "XXX", None]),
            "amount": st.one_of(
                st.decimals(min_value=-10, max_value=10**6, places=2).map(str), valid_or_junk
            ),
            "status": st.sampled_from(["paid", "SHIPPED", "lost", None]),
        }
    ),
    max_size=40,
)


@settings(max_examples=300, deadline=None)
@given(rows_strategy)
def test_every_row_is_accounted_for_and_ids_are_unique(rows: list[dict[str, str | None]]) -> None:
    r = transform(CUSTOMERS, rows, FX)
    assert r.accounted_for()
    ids = [o.order_id for o in r.orders]
    assert len(ids) == len(set(ids))
    assert all(o.amount_minor >= 0 and o.usd_cents >= 0 and o.order_ts.tzinfo == UTC for o in r.orders)


@settings(max_examples=100, deadline=None)
@given(rows_strategy)
def test_transform_is_deterministic(rows: list[dict[str, str | None]]) -> None:
    assert transform(CUSTOMERS, rows, FX) == transform(CUSTOMERS, rows, FX)
