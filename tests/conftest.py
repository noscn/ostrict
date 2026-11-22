from decimal import Decimal
from pathlib import Path

import pytest

from ostrict.connectors.duckdb import DuckDBConnector
from ostrict.parsers import load_view

FIXTURES = Path(__file__).parent / "fixtures"
JAN = [
    {"field": "order_date", "op": ">=", "value": "2026-01-01"},
    {"field": "order_date", "op": "<", "value": "2026-02-01"},
]


@pytest.fixture(scope="session")
def view():
    return load_view(FIXTURES / "ecommerce.sql")


@pytest.fixture(scope="session")
def connector():
    return DuckDBConnector(seed_sql=(FIXTURES / "seed_ecommerce.sql").read_text())


def normalize(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        out.append({k: float(v) if isinstance(v, Decimal) else v for k, v in r.items()})
    return out
