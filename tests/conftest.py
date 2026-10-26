from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest

from dqpipe.cli import read_csv, read_fx
from dqpipe.load import load
from dqpipe.transform import transform

ROOT = Path(__file__).parents[1]
SAMPLE = ROOT / "data" / "sample"
RUN_AT = datetime(2026, 10, 26, tzinfo=UTC)
FX: dict[str, tuple[Decimal, int]] = {
    "USD": (Decimal(1), 2),
    "EUR": (Decimal("1.08"), 2),
    "JPY": (Decimal("0.0066"), 0),
}


def generate(out: Path, *args: str) -> Path:
    subprocess.run([sys.executable, str(ROOT / "scripts" / "generate.py"), str(out), *args], check=True)
    return out


@pytest.fixture
def loaded_db() -> Iterator[Any]:
    """The committed sample dataset, loaded into an in-memory DuckDB as run 'r1'."""
    con = duckdb.connect()
    fx = read_fx(SAMPLE / "fx_rates.csv")
    result = transform(read_csv(SAMPLE / "customers.csv"), read_csv(SAMPLE / "orders.csv"), fx)
    load(con, result, fx, "r1", RUN_AT)
    yield con
    con.close()
