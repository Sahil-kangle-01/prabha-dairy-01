from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db import engine
from database.models import Base


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(autouse=True)
def _clean_tables():
    with engine.begin() as conn:
        conn.exec_driver_sql("TRUNCATE TABLE purchase_milk, sync_runs RESTART IDENTITY;")
    yield
